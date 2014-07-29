#!/usr/bin/env python2

# Copyright (c) 2014. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script lets you count the number of immunogenic mutations in a collection of cancer mutation files (in either MAF or VCF formats). 
Each mutation file is expect to have a corresponding .hla file containing the patient's HLA alleles. The output format is 
a CSV file with the following fields: 
  - patient_id (assumed to be base of each VCF/MAF file)
  - number of coding mutations
  - number of coding mutations which contained MHC epitopes
  - nubmer of coding mutations whose MHC epitopes are expected to be immunogenic

Example usage:
  python analyze_cohort.py --input-dir ../canseq/ --hla-input-dir ../canseq-hla/ --output results.csv
""" 

import argparse 
import logging
from os import listdir
from os.path import join, split, splitext
from collections import OrderedDict

from common import init_logging
from immunogenicity import ImmunogenicityPredictor
from load_file import load_file
from mhc_common import normalize_hla_allele_name
from mhc_netmhcpan import PanBindingPredictor
from mutation_report import print_mutation_report

parser = argparse.ArgumentParser()

parser.add_argument("--input-dir",
      type = str, 
      required = True, 
    help="Directory containing MAF or VCF input files")

parser.add_argument("--hla-input-dir",
    type = str, 
    default = None, 
    help = "Directory containing HLA allele files (with suffix .hla), if omitted assumed to be same as input-dir")

parser.add_argument("--output",
    default = None, 
    help = "Path to output file")


parser.add_argument("--quiet",
    type = str, 
    help = "Suppress INFO log messages")


parser.add_argument("--binding-threshold",
    type = int, 
    default = 500, 
    help = "Cutoff IC50 score for epitope MHC binding")


MUTATION_FILE_EXTENSIONS = [".maf", ".vcf"]

def find_mutation_files(input_dir_string):
    """
    Collect all .vcf/.maf file paths in the dir(s) given as a comma-separated string.
    Returns a dictionary mapping base filenames to full paths. 
    """
    mutation_files = OrderedDict()
    for dirpath in input_dir_string.split(","):
        for filename in listdir(dirpath):
            path = join(dirpath, filename)
            patient_id, ext = splitext(filename)
            if ext in MUTATION_FILE_EXTENSIONS:
                logging.info("Reading mutation file %s", path)
                assert patient_id not in mutation_files, \
                    "Duplicate files for %s: %s and %s" % (patient_id, mutation_files[patient_id], path)
                mutation_files[patient_id] = path
    return mutation_files

def find_hla_files(input_dir_string):
    """
    Collect all .hla files  in the dir(s) given as a comma-separated string, 
    read in all the HLA alleles and normalize them. 

    Returns a dictionary mapping base filenames to lists of HLA allele names. 
    """
    
    hla_types = {}
    for dirpath in input_dir_string.split(","):
        for filename in listdir(dirpath):
            patient_id, ext = splitext(filename)
            if ext == '.hla':
                path = join(dirpath, filename)
                logging.info("Reading HLA file %s", path)
                assert patient_id not in hla_types, "Duplicate HLA files for %s" % patient_id 
                alleles = []
                with open(path, 'r') as f:
                    contents = f.read()
                    for line in contents.split("\n"):
                        for raw_allele_name in line.split(","):
                            alleles.append(normalize_hla_allele_name(raw_allele_name))
                hla_types[patient_id] = alleles
    return hla_types

def generate_mutation_counts(mutation_files, hla_types):
    """
    Returns dictionary that maps each patient ID to a tuple with three fields:
        - number of mutated genes
        - number of mutated genes with MHC binding mutated epitope
        - number of mutated genes with immunogenic mutated epitope
    """
    mutation_counts = OrderedDict()
    for patient_id, path in mutation_files.iteritems():
        hla_allele_names = hla_types[patient_id]
        logging.info("Processing %s with HLA alleles %s", path, hla_allele_names)
        transcripts_df, raw_genomic_mutation_df, variant_report = load_file(path)

        # print each genetic mutation applied to each possible transcript
        # and either why it failed or what protein mutation resulted
        if not args.quiet:
            print_mutation_report(path, variant_report, raw_genomic_mutation_df, transcripts_df)

        mhc = PanBindingPredictor(hla_allele_names)
        imm = ImmunogenicityPredictor(
            alleles = hla_allele_names, 
            binding_threshold = args.binding_threshold)

        scored_epitopes = mhc.predict(transcripts_df)
        scored_epitopes = imm.predict(scored_epitopes)

        grouped = scored_epitopes.groupby(["Gene", "GeneMutationInfo"])
        n_coding_mutations = len(grouped)
        n_ligand_mutations = 0
        n_immunogenic_mutations = 0
        for (gene, mut), group in grouped:
            start_mask = group.EpitopeStart <= group.MutationEnd
            stop_mask = group.EpitopeEnd >= group.MutationStart 
            mutated_epitopes = group[start_mask & stop_mask]
            # we might have duplicate epitopes from multiple transcripts, so drop them
            mutated_epitopes = mutated_epitopes.groupby(['Epitope']).first()
            ligands = mutated_epitopes[mutated_epitopes.MHC_IC50 <= args.binding_threshold]
            n_ligand_mutations += len(ligands) > 0 
            immunogenic_epitopes = ligands[~ligands.ThymicDeletion]
            n_immunogenic_mutations += len(immunogenic_epitopes) > 0
            logging.info("%s %s: epitopes %s, ligands %d, imm %d", 
                    gene,
                    mut, 
                    len(mutated_epitopes),
                    len(ligands),
                    len(immunogenic_epitopes))
        mutation_counts[patient_id] = (n_coding_mutations, n_ligand_mutations, n_immunogenic_mutations)
    return mutation_counts

if __name__ == "__main__":
    args = parser.parse_args()

    init_logging(args.quiet)
    mutation_files = find_mutation_files(args.input_dir)

    # if no HLA input dir is specified then assume .hla files in the same dir
    # as the .maf/.vcf files 
    hla_dir_arg = args.hla_input_dir if args.hla_input_dir else args.input_dir
    hla_types = find_hla_files(hla_dir_arg)

    # make sure we have HLA types for each patient
    for patient_id, path in mutation_files.iteritems():
        assert patient_id in hla_types, "Missing HLA types for %s (%s)" % (patient_id, path)

    mutation_counts = generate_mutation_counts(mutation_files, hla_types)
    
    if args.output:
        output_file = open(args.output, 'w')
    else:
        output_file = None
    
    print
    print "SUMMARY"    
    for patient_id, (n_coding_mutations, n_ligand_mutations, n_immunogenic_mutations) in mutation_counts.iteritems():
        print "%s: # mutations %d, # mutations with ligands %d, # immunogenic mutations %d" % (
            patient_id, 
            n_coding_mutations, 
            n_ligand_mutations, 
            n_immunogenic_mutations
        )
        if output_file:
            output_file.write("%s,%d,%d,%d\n" % (patient_id, 
                n_coding_mutations, 
                n_ligand_mutations, 
                n_immunogenic_mutations)
            )
    if output_file:
        output_file.close()
        