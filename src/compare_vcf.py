import os
import subprocess
from argparse import ArgumentParser, SUPPRESS

from shared.vcf import VcfReader, VcfWriter
from shared.interval_tree import bed_tree_from, is_region_in
from shared.utils import file_path_from
major_contigs_order = ["chr" + str(a) for a in list(range(1, 23)) + ["X", "Y"]] + [str(a) for a in
                                                                                   list(range(1, 23)) + ["X", "Y"]]

major_contigs = {"chr" + str(a) for a in list(range(1, 23)) + ["X", "Y"]}.union(
    {str(a) for a in list(range(1, 23)) + ["X", "Y"]})

def cal_metrics(tp, fp, fn):
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1_score = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
    return round(precision, 6), round(recall, 6), round(f1_score, 6)


def compare_vcf(args):
    """
    How som.py works
    """
    output_fn = args.output_fn
    output_dir = args.output_dir
    truth_vcf_fn = args.truth_vcf_fn
    input_vcf_fn = args.input_vcf_fn
    bed_fn = args.bed_fn
    ref_fn = args.ref_fn
    high_confident_only = args.high_confident_only
    ctg_name = args.ctg_name
    skip_genotyping = args.skip_genotyping
    input_filter_tag = args.input_filter_tag
    truth_filter_tag = args.truth_filter_tag
    remove_fn_out_of_fp_bed = args.remove_fn_out_of_fp_bed
    fp_bed_tree = bed_tree_from(bed_file_path=bed_fn, contig_name=ctg_name)
    truth_vcf_fn = file_path_from(file_name=truth_vcf_fn, exit_on_not_found=True, allow_none=False)
    input_vcf_fn = file_path_from(file_name=input_vcf_fn, exit_on_not_found=True, allow_none=False)

    truth_vcf_reader = VcfReader(vcf_fn=truth_vcf_fn,
                                 ctg_name=ctg_name,
                                 ctg_start=args.ctg_start,
                                 ctg_end=args.ctg_end,
                                 show_ref=False,
                                 keep_row_str=True,
                                 skip_genotype=skip_genotyping,
                                 filter_tag=truth_filter_tag)
    truth_vcf_reader.read_vcf()
    truth_variant_dict = truth_vcf_reader.variant_dict

    input_vcf_reader = VcfReader(vcf_fn=input_vcf_fn,
                                 ctg_name=ctg_name,
                                 ctg_start=args.ctg_start,
                                 ctg_end=args.ctg_end,
                                 show_ref=False,
                                 keep_row_str=True,
                                 skip_genotype=skip_genotyping,
                                 filter_tag=input_filter_tag,
                                 discard_indel=True)
    input_vcf_reader.read_vcf()
    input_variant_dict = input_vcf_reader.variant_dict

    strat_bed_tree_list = []
    if args.strat_bed_fn is not None and ',' in args.strat_bed_fn:
        for strat_bed_fn in args.strat_bed_fn.split(','):
            strat_bed_tree_list.append(bed_tree_from(bed_file_path=strat_bed_fn, contig_name=ctg_name))
    elif args.strat_bed_fn is not None:
        strat_bed_tree_list = [bed_tree_from(bed_file_path=args.strat_bed_fn, contig_name=ctg_name)]

    low_qual_truth = set()
    if high_confident_only:
        for key in list(truth_variant_dict.keys()):
            row = truth_variant_dict[key].row_str
            if "PASS;HighConf" not in row:
                low_qual_truth.add(key)

    if output_fn:
        output_file = open(output_fn, 'w')
    else:
        output_file = None


    input_out_of_bed = 0
    truth_out_of_bed = 0
    
    for key in list(input_variant_dict.keys()):
        pos = key if ctg_name is not None else key[1]
        contig = ctg_name if ctg_name is not None else key[0]
        pass_bed_region = len(fp_bed_tree) == 0 or is_region_in(tree=fp_bed_tree,
                                                                contig_name=contig,
                                                                region_start=pos - 1,
                                                                region_end=pos)


        if not pass_bed_region:
            del input_variant_dict[key]
            input_out_of_bed += 1
            continue

        pass_straed_region = len(strat_bed_tree_list) == 0 or sum([1 if is_region_in(tree=strat_bed_tree,
                                                                contig_name=contig,
                                                                region_start=pos - 1,
                                                                region_end=pos) else 0 for strat_bed_tree in strat_bed_tree_list]) == len(strat_bed_tree_list)

        if not pass_straed_region and key in input_variant_dict:
            del input_variant_dict[key]
            input_out_of_bed += 1
            continue

        if high_confident_only and key in low_qual_truth:
            continue

    for key in list(truth_variant_dict.keys()):
        pos = key if ctg_name is not None else key[1]
        contig = ctg_name if ctg_name is not None else key[0]
        pass_bed_region = len(fp_bed_tree) == 0 or is_region_in(tree=fp_bed_tree,
                                                                contig_name=contig,
                                                                region_start=pos - 1,
                                                                region_end=pos)
        if not pass_bed_region:
            truth_out_of_bed += 1
            del truth_variant_dict[key]
            continue

        if high_confident_only and key in low_qual_truth:
            continue

        pass_straed_region = len(strat_bed_tree_list) == 0 or sum([1 if is_region_in(tree=strat_bed_tree,
                                                                contig_name=contig,
                                                                region_start=pos - 1,
                                                                region_end=pos) else 0 for strat_bed_tree in strat_bed_tree_list]) == len(strat_bed_tree_list)


        if not pass_straed_region and key in truth_variant_dict:
            del truth_variant_dict[key]
            input_out_of_bed += 1
            continue

    tp_snv, tp_ins, tp_del, fp_snv, fp_ins, fp_del, fn_snv, fn_ins, fn_del, fp_snv_truth, fp_ins_truth, fp_del_truth = 0,0,0,0,0,0,0,0,0,0,0,0
    truth_set = set()
    truth_snv, truth_ins, truth_del = 0,0,0
    query_snv, query_ins, query_del = 0,0,0
    pos_out_of_bed = 0

    fp_set = set()
    fn_set = set()
    fp_fn_set = set()
    tp_set = set()
    for key, vcf_infos in input_variant_dict.items():
        pos = key if ctg_name is not None else key[1]
        contig = ctg_name if ctg_name is not None else key[0]
        pass_bed_region = len(fp_bed_tree) == 0 or is_region_in(tree=fp_bed_tree,
                                                    contig_name=contig,
                                                    region_start=pos-1,
                                                    region_end=pos)
        if not pass_bed_region:
            pos_out_of_bed += 1
            # print(pos)
            continue

        if high_confident_only and key in low_qual_truth:
            continue

        ref_base = vcf_infos.reference_bases
        alt_base = vcf_infos.alternate_bases[0]
        genotype = vcf_infos.genotype
        qual = vcf_infos.qual
        is_snv = len(ref_base) == 1 and len(alt_base) == 1
        is_ins = len(ref_base) < len(alt_base)
        is_del = len(ref_base) > len(alt_base)

        if key not in truth_variant_dict and genotype != (0, 0):
            fp_snv = fp_snv + 1 if is_snv else fp_snv
            fp_ins = fp_ins + 1 if is_ins else fp_ins
            fp_del = fp_del + 1 if is_del else fp_del
            if fp_snv:
                fp_set.add(key)

        if key in truth_variant_dict:
            vcf_infos = truth_variant_dict[key]
            truth_ref_base = vcf_infos.reference_bases
            truth_alt_base = vcf_infos.alternate_bases[0]
            truth_genotype = vcf_infos.genotype
            is_snv_truth = len(truth_ref_base) == 1 and len(truth_alt_base) == 1
            is_ins_truth = len(truth_ref_base) < len(truth_alt_base)
            is_del_truth = len(truth_ref_base) > len(truth_alt_base)

            if genotype == (0, 0) and truth_genotype == (0, 0):
                continue

            genotype_match = skip_genotyping or (truth_genotype == genotype)
            if truth_ref_base == ref_base and truth_alt_base == alt_base and genotype_match:
                tp_snv = tp_snv + 1 if is_snv else tp_snv
                tp_ins = tp_ins + 1 if is_ins else tp_ins
                tp_del = tp_del + 1 if is_del else tp_del
                if tp_snv or is_snv_truth:
                    tp_set.add(key)
            else:
                fp_snv = fp_snv + 1 if is_snv else fp_snv
                fp_ins = fp_ins + 1 if is_ins else fp_ins
                fp_del = fp_del + 1 if is_del else fp_del

                fn_snv = fn_snv + 1 if is_snv_truth else fn_snv
                fn_ins = fn_ins + 1 if is_ins_truth else fn_ins
                fn_del = fn_del + 1 if is_del_truth else fn_del

                if fn_snv or fp_snv:
                    fp_fn_set.add(key)

            truth_set.add(key)

    for key, vcf_infos in truth_variant_dict.items():
        pos = key if ctg_name is not None else key[1]
        contig = ctg_name if ctg_name is not None else key[0]
        pass_bed_region = len(fp_bed_tree) == 0 or is_region_in(tree=fp_bed_tree,
                                                              contig_name=contig,
                                                              region_start=pos - 1,
                                                              region_end=pos)

        if key in truth_set:
            continue
        if not pass_bed_region and args.remove_fn_out_of_fp_bed:
            continue

        if high_confident_only and key in low_qual_truth:
            continue

        truth_ref_base = vcf_infos.reference_bases
        truth_alt_base = vcf_infos.alternate_bases[0]
        truth_genotype = vcf_infos.genotype
        if truth_genotype == (0, 0):
            continue
        is_snv_truth = len(truth_ref_base) == 1 and len(truth_alt_base) == 1
        is_ins_truth = len(truth_ref_base) < len(truth_alt_base)
        is_del_truth = len(truth_ref_base) > len(truth_alt_base)

        fn_snv = fn_snv + 1 if is_snv_truth else fn_snv
        fn_ins = fn_ins + 1 if is_ins_truth else fn_ins
        fn_del = fn_del + 1 if is_del_truth else fn_del

        if fn_snv:
            fn_set.add(key)

    pos_intersection = len(set(truth_variant_dict.keys()).intersection(set(input_variant_dict.keys())))
    print (pos_intersection, len(fp_set), len(fn_set), len(fp_fn_set), len(tp_set), len(fp_set.intersection(fn_set)))

    truth_indel = truth_ins + truth_del
    query_indel = query_ins + query_del
    tp_indel = tp_ins + tp_del
    fp_indel = fp_ins + fp_del
    fn_indel = fn_ins + fn_del
    truth_all = truth_snv + truth_indel
    query_all = query_snv + query_indel
    tp_all = tp_snv + tp_indel
    fp_all = fp_snv + fp_indel
    fn_all = fn_snv + fn_indel

    all_pre, all_rec, all_f1 = cal_metrics(tp=tp_all, fp=fp_all, fn=fn_all)
    snv_pre, snv_rec, snv_f1 = cal_metrics(tp=tp_snv, fp=fp_snv, fn=fn_snv)
    indel_pre, indel_rec, indel_f1 = cal_metrics(tp=tp_indel, fp=fp_indel, fn=fn_indel)
    ins_pre, ins_rec, ins_f1 = cal_metrics(tp=tp_ins, fp=fp_ins, fn=fn_ins)
    del_pre, del_rec, del_f1 = cal_metrics(tp=tp_del, fp=fp_del, fn=fn_del)

    # print (tp_snv, tp_ins, tp_del, fp_snv, fp_ins, fp_del, fn_snv, fn_ins, fn_del, fp_snv_truth, fp_ins_truth, fp_del_truth)
    print ((ctg_name + '-' if ctg_name is not None else "") + input_vcf_fn.split('/')[-1])
    print (len(input_variant_dict), len(truth_variant_dict), pos_out_of_bed)

    print (''.join([item.ljust(15) for item in ["Type", 'TP', 'FP', 'FN', 'Precision', 'Recall', "F1-score"]]), file=output_file)
    print (''.join([str(item).ljust(15) for item in ["SNV", tp_snv, fp_snv, fn_snv, snv_pre, snv_rec, snv_f1]]),file=output_file)
    if args.benchmark_indel:
        print (''.join([str(item).ljust(15) for item in ["INDEL", truth_indel, query_indel, tp_indel, fp_indel, fn_indel, indel_pre, indel_rec, indel_f1]]), file=output_file)
        print (''.join([str(item).ljust(15) for item in ["INS", truth_ins, query_ins, tp_ins, fp_ins, fn_ins, ins_pre, ins_rec, ins_f1]]), file=output_file)
        print (''.join([str(item).ljust(15) for item in ["DEL", query_del, query_del, tp_del, fp_del, fn_del, del_pre, del_rec, del_f1]]), file=output_file)


    if args.roc_fn:
        fp_dict = dict([(key, float(input_variant_dict[key].qual)) for key in fp_set])
        tp_dict = dict([(key, float(input_variant_dict[key].qual)) for key in tp_set])
        qual_list = sorted([float(qual) for qual in fp_dict.values()] + [qual for qual in tp_dict.values()],
                           reverse=True)

        tp_count = len(tp_set)
        roc_fn = open(args.roc_fn, 'w')
        for qual_cut_off in qual_list:
            pass_fp_count = sum([1 if float(qual) >= qual_cut_off else 0 for key, qual in fp_dict.items()])
            pass_tp_count = sum([1 if float(qual) >= qual_cut_off else 0 for key, qual in tp_dict.items()])
            fn_count = tp_count - pass_tp_count + fn_snv
            tmp_pre, tmp_rec, tmp_f1 = cal_metrics(tp=pass_tp_count, fp=pass_fp_count, fn=fn_count)
            roc_fn.write('\t'.join([str(item) for item in [qual_cut_off, tmp_pre, tmp_rec, tmp_f1]]) + '\n')
        roc_fn.close()

    if args.log_som is not None and os.path.exists(args.log_som):
            log_som = open(args.log_som)
            for row in log_som.readlines():
                if 'SNVs' not in row:
                    continue

                columns = row.rstrip().split(',')
                # total_truth, total_query = [float(item) for item in columns[2:4]]
                tp, fp, fn, unk, ambi = [float(item) for item in columns[4:9]]
                recall,recall_lower, recall_upper, recall2 = [float(item) for item in columns[9:13]]
                precision, precision_lower, precision_upper = [float(item) for item in columns[13:16]]
                # na, ambiguous, fp_region_size, fp_rate = [float(item) for item in  columns[16:20]]
                if int(tp_snv) != int(tp):
                    print("True positives not match")
                if int(fp_snv) != int(fp):
                    print("False positives not match")
                if int(fn_snv) != int(fn):
                    print("False negatives not match")

                print(fp, fn, tp, precision, recall)

    if output_dir is not None:
        if not os.path.exists(output_dir):
            subprocess.run("mkdir -p {}".format(output_dir), shell=True)
        candidate_types = ['fp', 'fn', 'fp_fn', 'tp']
        variant_sets = [fp_set, fn_set, fp_fn_set, tp_set]
        for vcf_type, variant_set in zip(candidate_types, variant_sets):
            vcf_fn = os.path.join(output_dir, '{}.vcf'.format(vcf_type))
            vcf_writer = VcfWriter(vcf_fn=vcf_fn, ctg_name=ctg_name, write_header=False)
            pos = key if ctg_name is not None else key[1]

            for key in variant_set:
                if key in input_variant_dict:
                    vcf_infos = input_variant_dict[key]
                elif key in truth_variant_dict:
                    vcf_infos = truth_variant_dict[key]
                else:
                    continue

                vcf_writer.write_row(row_str=vcf_infos.row_str)
            vcf_writer.close()
    if output_fn:
        output_file.close()



def main():
    parser = ArgumentParser(description="Compare input VCF with truth VCF")

    parser.add_argument('--output_fn', type=str, default=None,
                        help="Output VCF filename, required")

    parser.add_argument('--bed_fn', type=str, default=None,
                        help="High confident Bed region for benchmarking")

    parser.add_argument('--input_vcf_fn', type=str, default=None,
                        help="Input vcf filename")

    parser.add_argument('--truth_vcf_fn', type=str, default=None,
                        help="Truth vcf filename")

    parser.add_argument('--ref_fn', type=str, default=None,
                        help="Reference fasta file input")

    parser.add_argument('--ctg_name', type=str, default=None,
                        help="Contigs file with all processing contigs")

    parser.add_argument('--ctg_start', type=int, default=None,
                        help="The 1-based starting position of the sequence to be processed")

    parser.add_argument('--ctg_end', type=int, default=None,
                        help="The 1-based ending position of the sequence to be processed,")

    parser.add_argument('--contigs_fn', type=str, default=None,
                        help="Contigs file with all processing contigs")

    parser.add_argument('--output_dir', type=str, default=None,
                        help="Output directory")

    parser.add_argument('--skip_genotyping', action='store_true',
                        help="Skip calculating VCF genotype")

    parser.add_argument('--input_filter_tag', type=str, default=None,
                        help="Filter tag for the input VCF")

    parser.add_argument('--truth_filter_tag', type=str, default=None,
                        help="Filter tag for the truth VCF")

    ## Only benchmark 'HighConf' tag in seqc VCF
    parser.add_argument('--high_confident_only', type=str, default=None,
                        help=SUPPRESS)

    parser.add_argument('--remove_fn_out_of_fp_bed', type=str, default=None,
                        help=SUPPRESS)

    parser.add_argument('--roc_fn', type=str, default=None,
                        help=SUPPRESS)

    parser.add_argument('--log_som', type=str, default=None,
                        help=SUPPRESS)

    parser.add_argument('--caller', type=str, default=None,
                        help=SUPPRESS)

    parser.add_argument('--output_best_f1_score', action='store_true',
                        help=SUPPRESS)

    parser.add_argument('--benchmark_indel', action='store_true',
                        help=SUPPRESS)

    parser.add_argument('--strat_bed_fn', type=str, default=None,
                        help="Genome stratifications v2 bed region")

    args = parser.parse_args()

    compare_vcf(args)

if __name__ == "__main__":
    main()
