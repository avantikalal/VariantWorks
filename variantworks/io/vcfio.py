#
# Copyright 2020 NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Classes for reading and writing VCFs."""


from collections import defaultdict
import vcf
import pandas as pd
import cyvcf2

import multiprocessing as mp
from functools import partial

from variantworks.io.baseio import BaseReader
from variantworks.types import VariantZygosity, VariantType, Variant
from variantworks.utils import extend_exception


class VCFReader(BaseReader):
    """Reader for VCF files."""

    def __init__(self, vcf, bams=[], is_fp=False, require_genotype=True, tag="caller", info_keys=[], filter_keys=[], format_keys=["*"], regions=None, num_threads=mp.cpu_count(), chunksize=5000):
        """Parse and extract variants from a vcf/bam tuple.

        Note VCFReader splits multi-allelic entries into separate variant
        entries. VCFReader also doesn't maintain the original ordering of variants
        from the VCF becasue of its multi threaded nature for parsing.

        Args:
            vcf : Path to VCF file.
            bams : List of BAMs corresponding to the VCF. BAM ordering should match sample
                   ordering in VCF.
            is_fp : Is the VCF for false positive variants.
            require_genotype : If all samples need genotype called.
            tag : Tag VCF data frame with. ["caller" by default]
            info_keys : List of INFO columns to parse. For all columns, pass "*". [Empty by default].
            filter_keys : List of FILTER columns to parse. For all columns, pass "*". [Empty be default].
            format_keys : List of FORMAT columns to parse. For all columns, pass "*". ["*" by default].
            regions : Region of VCF to parse. Needs tabix index (vcf name + .tbi). Format = "chr:start-end,...". [None by default]
            num_threads : Number of threads to use for parallel parsing of VCF. [CPU count by default]
            chunksize : Number of VCF rows to parse in a single threads. [5000 by default].

        Returns:
           Instance of class.
        """
        super().__init__()
        self._vcf = vcf
        self._bams = bams
        self._is_fp = is_fp
        self._require_genotype = require_genotype
        self._tag = tag
        self._regions = regions
        self._num_threads = num_threads
        self._chunksize = chunksize

        self._dataframe = None
        self._sample_names = set()

        # Keep track of metadata per column
        self._info_vcf_keys = info_keys
        self._info_vcf_key_counts = dict()

        self._filter_vcf_keys = filter_keys

        self._format_vcf_keys = format_keys
        self._format_vcf_key_counts = dict()

        # Parse the VCF
        self._parallel_parse_vcf()


    def __getitem__(self, idx):
        """Get Variant instance in location.

        Args:
            idx: Variant index
        Returns:
            Variant instance
        """
        row = self._dataframe.iloc[idx]

        # Build sample data by iterating through FORMAT columns.
        samples = []
        zygosities = []
        format_keys = sorted(self._format_vcf_key_counts.keys())
        for call in self._sample_names:
            call_data = []
            for k in format_keys:
                count = self._format_vcf_key_counts[k]
                if count == 1:
                    call_data.append(row["{}_{}".format(call, k)])
                else:
                    for i in range(count):
                        call_data.append(row["{}_{}_{}".format(call, k, i)])
            samples.append(call_data)
            zygosities.append(VariantZygosity(row["{}_zyg".format(call)]))

        # Build filter data by iterating over all saved FILTER keys.
        var_filter = []
        for k in self._filter_vcf_keys:
            if row["FILTER_{}".format(k)]:
                var_filter.append(k)

        # Build info data by iterating over all saved INFO keys.
        info = {}
        for k, count in self._info_vcf_key_counts.items():
            if count == 1:
                info[k] = row["INFO_{}".format(k)]
            else:
                vals = []
                for i in range(count):
                    vals.append(row["INFO_{}_{}".format(k, i)])
                info[k] = vals

        variant = Variant(chrom=row["chrom"],
                          pos=row["start_pos"],
                          id=row["id"],
                          ref=row["ref"],
                          allele=row["alt"],
                          quality=row["quality"],
                          filter=(var_filter if var_filter else None),
                          info=info,
                          format=format_keys,
                          type=VariantType(row["variant_type"]),
                          samples=samples,
                          zygosity=zygosities,
                          vcf=self._vcf,
                          bams=self._bams)
        return variant

    def __len__(self):
        """Return number of Varint objects."""
        return len(self._dataframe)

    @property
    def df(self):
        """Get variant list as a CPU pandas dataframe.

        Each row in the returned dataframe represents a variant entry.
        For each variant entry, the following metrics are currently tracked -
        1. chrom - Chromosome
        2. start_pos - Start position of variant (inclusive)
        3. end_pos - End position of variant (exclusive)
        4. ref - Reference base(s)
        5. alt - Alternate base(s)
        6. variant_type - VariantType enum specifying SNP/INSERTION/DELETION as an int
        7. quality - Quality of variant call
        8. filter - VCF FILTER column [if requested]
        9. info - VCF INFO column [if requested]
        10. format - VCF FORMAT column [if requested]
        11. sample_{idx}_zyg - VariantZygosity enum for sample idx as an int

        This dataframe can be easily converted to cuDF for large scale
        variant processing.

        Returns:
            Parsed variants as pandas DataFrame.
        """
        if self._dataframe is None:
            raise RuntimeError("VCF data frame should be available.")
        return self._dataframe


    def _detect_variant_type(self, ref, alt):
        """Get variant type enum.

        Given ref and alt alleles, determine type of variant.
        
        Args:
            ref : ref bases
            alt : alt bases
        Returns:
            VariantType enum
        """
        if len(ref) == len(alt):
            return VariantType.SNP
        elif len(ref) < len(alt):
            return VariantType.INSERTION
        else:
            return VariantType.DELETION

    def _detect_zygosity(self, gt):
        """Get variant zygosity as enum.

        Given a diploid genotype type with genotype information per
        haploid, determine the zygosity of the sample. If the variant is known
        false positive, returns NO_VARIANT. If any of the alts are -1, then returns
        NONE (e.g. in multi allele cases that were split).

        Args:
            gt : Diploid genotype in the format [haploid 1 alt num, haploid 2 alt num]
        Returns:
            Relevant VariantZygosity enum.
        """
        if self._is_fp:
            return VariantZygosity.NO_VARIANT

        if gt[0] == -1 or gt[1] == -1:
            return VariantZygosity.NONE
        elif gt[0] == gt[1]:
            if gt[0] == 0:
                return VariantZygosity.NO_VARIANT
            else:
                return VariantZygosity.HOMOZYGOUS
        else:
            return VariantZygosity.HETEROZYGOUS

    def _get_normalized_count(self, header_number, num_alts, num_samples):
        """Calculate number of values for a VCF key based.

        Determine number of values based on header number, alt count and sample
        count.

        Args:
            header_number : VCF header number
            num_alts : Number of alt alleles for variant
            num_samples : Number of samples in VCF
        Returns:
            Integer number of values.
        """
        if header_number == "A":
            return num_alts
        elif header_number == "R":
            return (num_alts + 1)
        elif header_number == "G":
            return num_samples
        elif header_number.isdigit():
            return int(header_number)
        elif header_number == ".":
            return 1

    def _get_header_type_lambda(self, header_type):
        """Determine data type of header values.

        Based on data type mentioned in VCF header, generate lambda
        to convert str to relevant type.

        Args:
            header_type : VCF header type
        Returns:
            Lambda function for converting string to VCF type
        """
        if header_type == "String":
            return lambda x : None if x is None else str(x)
        elif header_type == "Integer":
            return lambda x : None if x is None else int(x)
        elif header_type == "Float":
            return lambda x : None if x is None else float(x)
        elif header_type == "Flag":
            return lambda x : False if x is None else True
        else:
            raise RuntimeError("Unknown VCF header type:", header_type)

    def _create_df(self, vcf, variant_list):
        """Create dataframe from list of cyvcf2.Variant objects.

        Process each cyvcf2.Variant object in the variant list and generate a dict
        with all the user specified VCF columns and their values. To simplify downstream
        processing, also split multi alleles into separate rows and update relevant info/format
        columns to account for the multi allele change.

        Args:
            vcf : cyvcf2 object for VCF
            variant_list : List of cyvcf2.Variant objects to convert to dataframe
        Returns:
            DataFrame for entries in variant_list
        """
        df_dict = defaultdict(list)

        samples = vcf.samples

        # Iterate over all variants in variant list.
        for variant in variant_list:
            # Iterate over each allele in variant to split up multi alleles.
            alts = variant.ALT
            for alt_idx, alt in enumerate(alts):
                # Add standard DF entries for each variant.
                df_dict["chrom"].append(variant.CHROM)
                df_dict["start_pos"].append(variant.start)
                df_dict["end_pos"].append(variant.end)
                df_dict["id"].append(variant.ID)
                df_dict["ref"].append(variant.REF)
                df_dict["alt"].append(alt)
                df_dict["variant_type"].append(int(self._detect_variant_type(variant.REF, alt)))
                df_dict["quality"].append(variant.QUAL)

                # Process variant filter columns. If filter is present in entry, store True else False.
                variant_filter = "PASS" if variant.FILTER is None else variant.FILTER
                filter_set = set(variant_filter.split(";"))
                for filter_col in self._filter_vcf_keys:
                    df_dict["FILTER_" + filter_col].append(filter_col in filter_set)

                # Process INFO columns. INFO column values need to be handled specially based on header number
                # and header type. Since multi alleles are split up, the right filter values need to go to the
                # right variant row.
                for info_col in self._info_vcf_keys:
                    # Get header type
                    header_number = vcf.get_header_type(info_col)['Number']
                    header_python_type = self._get_header_type_lambda(vcf.get_header_type(info_col)['Type'])

                    if info_col in variant.INFO:
                        val = variant.INFO[info_col]
                        # Make value a tuple to reduce special case handling later.
                        if not isinstance(val, tuple):
                            val = tuple((val,))
                    else:
                        val = [None] * self._get_normalized_count(header_number, len(alts), len(samples))

                    df_key = "INFO_" + info_col

                    if header_number == "A":
                        df_dict[df_key].append(val[alt_idx])
                    elif header_number == "R":
                        df_dict[df_key + "_REF"].append(header_python_type(val[0]))
                        df_dict[df_key + "_ALT"].append(header_python_type(val[alt_idx + 1]))
                    elif header_number.isdigit():
                        header_number = int(header_number)
                        if header_number == 1:
                            df_dict[df_key].append(header_python_type(val[0]))
                        else:
                            for i in range(int(header_number)):
                                df_dict[df_key + "_" + str(i)].append(header_python_type(val[i]))
                    elif header_number == ".":
                        df_dict[df_key].append(",".join([str(v) for v in val]))

                # Process format columns. Handle GT specially, and the rest can be handled like INFO columns.
                for format_col in self._format_vcf_keys:
                    for sample_idx, sample_name in enumerate(samples):
                        if format_col == "GT":
                            def fix_gt(gt_alt_id, loop_alt_id):
                                """Fix up genotype.

                                If gt alt id and loop alt id are the same, return 1 for alt.
                                If they're not 0, then it represents a split multi allele that's not handled in
                                the current loop.
                                If gt is 0, then return 0 as ref.

                                Args:
                                    gt_alt_id : ID of alt allele
                                    loop_alt_id : ID of current alt in loop
                                Returns:
                                    Fixed up ID of alt.
                                """
                                if gt_alt_id == loop_alt_id:
                                    return 1
                                elif gt_alt_id != 0:
                                    return -1
                                else:
                                    return 0
                            # Handle GT column specially
                            gt = variant.genotypes[sample_idx]
                            # Fixup haplotype number based on multi allele split.
                            alt_id = alt_idx + 1
                            gt[0] = fix_gt(gt[0], alt_id)
                            gt[1] = fix_gt(gt[1], alt_id)
                            if gt[0] == -1 or gt[1] == -1:
                                gt[0] = gt[1] = -1
                            df_dict["{}_zyg".format(sample_name)].append(int(self._detect_zygosity(gt)))
                            df_dict["{}_GT".format(sample_name)].append("{}/{}".format(gt[0], gt[1]))
                        else:
                            # Get header type
                            header_number = vcf.get_header_type(format_col)['Number']
                            header_python_type = self._get_header_type_lambda(vcf.get_header_type(format_col)['Type'])

                            val = variant.format(format_col)
                            if val is not None:
                                val = val[sample_idx]
                            else:
                                val = [None] * self._get_normalized_count(header_number, len(alts), len(samples))

                            df_key = sample_name + "_" + format_col

                            #print(format_col, val)
                            if header_number == "A":
                                df_dict[df_key].append(header_python_type(val[alt_idx]))
                            elif header_number == "R":
                                df_dict[df_key + "_REF"].append(header_python_type(val[0]))
                                df_dict[df_key + "_ALT"].append(header_python_type(val[alt_idx + 1]))
                            elif header_number.isdigit():
                                header_number = int(header_number)
                                if header_number == 1:
                                    df_dict[df_key].append(header_python_type(val[0]))
                                else:
                                    #print(header_number, format_col, val)
                                    for i in range(int(header_number)):
                                        df_dict[df_key + "_" + str(i)].append(header_python_type(val[i]))
                            elif header_number == ".":
                                df_dict[df_key].append(",".join([str(v) for v in val]))


        # Convert local dictionary of k/v to DataFrame.
        df = pd.DataFrame.from_dict(df_dict)
        return df


    def _parse_vcf_cyvcf(self, thread_id):
        """Parse portions of a VCF file as determined by chunk size and thread id.

        Based on the thread ID, split up a VCF file into equal sized chunks and distribute
        chunks in a round robin fashion to various threads. Each thread only processes
        variants that occur within its chunk. This distribution is handled within each
        thread indepdently. Each thread goes through all variants, and only processes the
        ones that fall in the variant index range determined by its thread id.

        Args:
           thread_id : Thead ID of VCF processing thread.

        Returns:
            DataFrame with all variants in the range of the parser.
        """
        vcf = cyvcf2.VCF(self._vcf)

        # Go through variants and add to list
        variant_list = []
        df_list = []
        generator = vcf(self._regions) if self._regions else vcf
        # Loop through all variants in cyvcf2 object.
        for idx, variant in enumerate(generator):
            # Check if a variant maps to this thread.
            if ((idx // self._chunksize) % self._num_threads == thread_id):
                variant_list.append(variant)
                if idx % self._chunksize == 0:
                    df_list.append(self._create_df(vcf, variant_list))
                    variant_list = []
                    #print("Processed", idx, "variants")
        if variant_list:
            df_list.append(self._create_df(vcf, variant_list))

        if df_list:
            return pd.concat(df_list, ignore_index=True)
        else:
            return pd.DataFrame()

    def _parallel_parse_vcf(self):
        """Parse VCF file in multi threaded fashion.

        Split the work of parsing a VCF file among multiple threads each of which
        generate a DataFrame with subsections of the main VCF, and then concatenate them
        together to form the final VCF.

        The final DataFrame does now guarantee any ordering of the variants. It only guarantees
        the presence of all variants from the VCF.
        """
        vcf = cyvcf2.VCF(self._vcf)

        # Populate column keys and the number of values for them. Do this for INFO, FILTER
        # and FORMAT keys.
        if "*" in self._info_vcf_keys:
            self._info_vcf_keys = []
            for h in vcf.header_iter():
                if h['HeaderType'] == 'INFO':
                    self._info_vcf_keys.append(h['ID'])
        for k in self._info_vcf_keys:
            header_number = vcf.get_header_type(k)['Number']
            self._info_vcf_key_counts[k] = self._get_normalized_count(header_number, 1, len(vcf.samples))

        if "*" in self._filter_vcf_keys:
            self._filter_vcf_keys = []
            for h in vcf.header_iter():
                if h['HeaderType'] == 'FILTER':
                    self._filter_vcf_keys.append(h['ID'])

        if "*" in self._format_vcf_keys:
            self._format_vcf_keys = []
            for h in vcf.header_iter():
                if h['HeaderType'] == 'FORMAT':
                    self._format_vcf_keys.append(h['ID'])
        for k in self._format_vcf_keys:
            header_number = vcf.get_header_type(k)['Number']
            self._format_vcf_key_counts[k] = self._get_normalized_count(header_number, 1, len(vcf.samples))

        # Store name of samples in VCF.
        for sample in vcf.samples:
            self._sample_names.add(sample)

        # Create a pool of threads and distribute parsing to multiple threads.
        pool = mp.Pool(self._num_threads)
        df_list = []
        func = partial(self._parse_vcf_cyvcf)
        for df in pool.imap(func, range(self._num_threads)):
            df_list.append(df)

        # Generate final DataFrame from intermediate DataFrames computed by
        # individual threads.
        self._dataframe = pd.concat(df_list, ignore_index=True)
