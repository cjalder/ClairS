import logging
import random
import numpy as np
from argparse import ArgumentParser, SUPPRESS
import tensorflow_addons as tfa
import tensorflow as tf
import tables
import os
import sys
from itertools import accumulate

import clair_somatic.model as model_path
from shared.utils import str2bool
import shared.param as param
logging.basicConfig(format='%(message)s', level=logging.INFO)
tables.set_blosc_max_threads(512)
os.environ['NUMEXPR_MAX_THREADS'] = '64'
os.environ['NUMEXPR_NUM_THREADS'] = '8'


def get_label_task(label, label_shape_cum, task):
    if task == 0:
        return label[:label_shape_cum[task]]
    elif task == len(label_shape_cum) - 1:
        return label[label_shape_cum[task - 1]:]
    else:
        return label[label_shape_cum[task - 1]:label_shape_cum[task]]


def cal_class_weight(samples_per_cls, no_of_classes, beta=0.999):
    effective_num = 1.0 - np.power(beta, samples_per_cls)
    cls_weights = (1.0 - beta) / np.array(effective_num)
    cls_weights = cls_weights / np.sum(cls_weights) * no_of_classes
    return cls_weights


class FocalLoss(tf.keras.losses.Loss):
    """
    updated version of focal loss function, for multi class classification, we remove alpha parameter, which the loss
    more stable, and add gradient clipping to avoid gradient explosion and precision overflow.
    """

    def __init__(self, label_shape_cum, task, effective_label_num=None, gamma=2):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.cls_weights = None
        if effective_label_num is not None:
            task_label_num = get_label_task(effective_label_num, label_shape_cum, task)
            cls_weights = cal_class_weight(task_label_num, len(task_label_num))
            cls_weights = tf.constant(cls_weights, dtype=tf.float32)
            cls_weights = tf.expand_dims(cls_weights, axis=0)
            self.cls_weights = cls_weights

    def call(self, y_true, y_pred):
        y_pred = tf.clip_by_value(y_pred, clip_value_min=1e-9, clip_value_max=1 - 1e-9)
        cross_entropy = -y_true * tf.math.log(y_pred)
        weight = ((1 - y_pred) ** self.gamma) * y_true
        FCLoss = cross_entropy * weight
        if self.cls_weights is not None:
            FCLoss = FCLoss * self.cls_weights
        reduce_fl = tf.reduce_sum(FCLoss, axis=-1)
        return reduce_fl

class BinaryCrossentropy(tf.keras.losses.Loss):
    """
    updated version of focal loss function, for multi class classification, we remove alpha parameter, which the loss
    more stable, and add gradient clipping to avoid gradient explosion and precision overflow.
    """

    def __init__(self):
        super(BinaryCrossentropy, self).__init__()

    def call(self, y_true, y_pred):
        sigmoids = tf.nn.sigmoid_cross_entropy_with_logits(labels=y_true, logits=y_pred)
        sigmoids_loss = tf.reduce_mean(sigmoids)
        return sigmoids_loss


def get_chunk_list(chunk_offset, train_data_size, chunk_size):
    """
    get chunk list for training and validation data. we will randomly split training and validation dataset,
    all training data is directly acquired from various tensor bin files.

    """
    all_shuffle_chunk_list = []
    total_size = 0
    offset_idx = 0
    for bin_idx, chunk_num in enumerate(chunk_offset):
        all_shuffle_chunk_list += [(bin_idx, chunk_idx) for chunk_idx in range(chunk_num)]
    np.random.seed(0)
    np.random.shuffle(all_shuffle_chunk_list)  # keep the same random validate dataset
    for bin_idx, chunk_num in enumerate(chunk_offset):
        if chunk_num * chunk_size + total_size >= train_data_size:
            chunk_num = (train_data_size - total_size) // chunk_size
            offset_idx += chunk_num
            # print ("Sum:{}".format(np.sum(np.array(all_shuffle_chunk_list[:offset_idx]))))
            return np.array(all_shuffle_chunk_list[:offset_idx]), np.array(all_shuffle_chunk_list[offset_idx + 1:])
        else:
            total_size += chunk_num * chunk_size
            offset_idx += chunk_num


def exist_file_prefix(exclude_training_samples, f):
    for prefix in exclude_training_samples:
        if prefix in f:
            return True
    return False


def pass_chr(fn, ctg_name_list):
    if ctg_name_list is None or len(ctg_name_list) == 0:
        return True
    in_testing_chr = False
    for ctg_name in ctg_name_list:
        if '_' + ctg_name + '.' in fn:
            return True
    return False

def compute_euclidean_distance(x, y):
    """
    Computes the euclidean distance between two tensorflow variables
    """

    d = tf.reduce_sum(tf.square(tf.sub(x, y)),1)
    return d



class ContrastiveLoss(tf.keras.losses.Loss):

    """
    Compute the contrastive loss as in


    L = 0.5 * Y * D^2 + 0.5 * (Y-1) * {max(0, margin - D)}^2
    Y=1: same class(similar), Y=0: different class
    **Parameters**
     left_feature: First element of the pair
     right_feature: Second element of the pair
     label: Label of the pair (0 or 1)
     margin: Contrastive margin

    **Returns**
     Return the loss operation

    """

    def __init__(self, margin=1):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin

    # def call(self, y_true, y_pred):
    #     label = tf.argmax(y_true, axis=1)
    #     label = tf.cast(label, tf.float32)
    #
    #     d_sqrt = tf.sqrt(y_pred)
    #     first_part = tf.multiply(1.0 - label, y_pred)  # (Y-1)*(d)
    #
    #     max_part = tf.square(tf.maximum(self.margin - d_sqrt, 0))
    #     second_part = tf.multiply(label, max_part)  # (Y) * max(margin - d, 0)
    #
    #     loss = 0.5 * tf.reduce_sum(first_part + second_part, axis=-1)
    #     return loss
    def call(self, y_true, y_pred):
        label = tf.argmax(y_true, axis=1)
        label = tf.cast(label, tf.float32)

        d_sqrt = tf.sqrt(y_pred)
        first_part = tf.multiply(1.0 - label, y_pred)  # (Y-1)*(d)

        max_part = tf.square(tf.maximum(self.margin - d_sqrt, 0))
        second_part = tf.multiply(label, max_part)  # (Y) * max(margin - d, 0)

        loss = 0.5 * tf.reduce_sum(first_part + second_part, axis=-1)
        return loss


def train_model(args):
    use_siam = args.use_siam
    add_contrastive = args.add_contrastive
    load_data_into_memory = False
    platform = args.platform
    pileup = args.pileup
    add_indel_length = args.add_indel_length
    ctg_name_string = args.ctgName
    use_siam = False
    add_contrastive = False
    ctg_name_list = ctg_name_string.split(',')  if ctg_name_string is not None else []
    exclude_training_samples = args.exclude_training_samples
    exclude_training_samples = set(exclude_training_samples.split(',')) if exclude_training_samples else set()
    add_validation_dataset = True
    ochk_prefix = args.ochk_prefix if args.ochk_prefix is not None else ""
    tensor_shape = param.ont_input_shape if platform == 'ont' else param.input_shape
    label_size, label_shape = param.label_size, param.label_shape

    label_shape = [2, 3] if use_siam else label_shape
    label_shape_cum = list(accumulate(label_shape))
    batch_size, chunk_size = param.trainBatchSize, param.chunk_size
    random.seed(param.RANDOM_SEED)
    np.random.seed(param.RANDOM_SEED)
    learning_rate = args.learning_rate if args.learning_rate else param.initialLearningRate
    max_epoch = args.maxEpoch if args.maxEpoch else param.maxEpoch
    task_num = 2 if add_contrastive else 1
    TensorShape = (tuple(tf.TensorShape([None] + tensor_shape) for _ in range(2)),
        tuple(tf.TensorShape([None, label_shape[task]]) for task in range(task_num)))

    TensorDtype = ((tf.int32, tf.int32), tuple(tf.float32 for _ in range(task_num)))

    bin_list = os.listdir(args.bin_fn)
    bin_list = [f for f in bin_list if pass_chr(f, ctg_name_list) and not exist_file_prefix(exclude_training_samples, f)]
    logging.info("[INFO] total {} training bin files: {}".format(len(bin_list), ','.join(bin_list)))
    total_data_size = 0
    table_dataset_list = []
    validate_table_dataset_list = []
    chunk_offset = np.zeros(len(bin_list), dtype=int)

    for bin_idx, bin_file in enumerate(bin_list):
        table_dataset = tables.open_file(os.path.join(args.bin_fn, bin_file), 'r')
        validate_table_dataset = tables.open_file(os.path.join(args.bin_fn, bin_file), 'r')
        table_dataset_list.append(table_dataset)
        validate_table_dataset_list.append(validate_table_dataset)
        chunk_num = (len(table_dataset.root.label) - batch_size) // chunk_size
        data_size = int(chunk_num * chunk_size)
        chunk_offset[bin_idx] = chunk_num
        total_data_size += data_size

    train_data_size = total_data_size * param.trainingDatasetPercentage
    validate_data_size = int((total_data_size - train_data_size) // chunk_size) * chunk_size
    train_data_size = int(train_data_size // chunk_size) * chunk_size
    train_shuffle_chunk_list, validate_shuffle_chunk_list = get_chunk_list(chunk_offset, train_data_size, chunk_size)

    def DataGenerator(x, data_size, shuffle_chunk_list, train_flag=True):

        chunk_iters = batch_size // chunk_size
        batch_num = data_size // batch_size
        normal_matrix = np.empty([batch_size] + tensor_shape, np.int32)
        tumor_matrix = np.empty([batch_size] + tensor_shape, np.int32)
        label = np.empty((batch_size, param.label_size), np.float32)

        random_start_position = np.random.randint(0, batch_size) if train_flag else 0
        if train_flag:
            np.random.shuffle(shuffle_chunk_list)
        for batch_idx in range(batch_num):
            # if not first_epoch and load_data_into_memory:

            for chunk_idx in range(chunk_iters):
                offset_chunk_id = shuffle_chunk_list[batch_idx * chunk_iters + chunk_idx]
                bin_id, chunk_id = offset_chunk_id
                normal_matrix[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size] = x[bin_id].root.normal_matrix[
                        random_start_position + chunk_id * chunk_size:random_start_position + (chunk_id + 1) * chunk_size]

                tumor_matrix[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size] = x[bin_id].root.tumor_matrix[
                        random_start_position + chunk_id * chunk_size:random_start_position + (chunk_id + 1) * chunk_size]

                label[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size] = x[bin_id].root.label[
                        random_start_position + chunk_id * chunk_size:random_start_position + (chunk_id + 1) * chunk_size]

            if add_contrastive:
                label_for_normal = [[0,1] if np.argmax(item) == 1 else [1,0] for item in label]
                label_for_tumor = [[0,0,1] if np.argmax(item) == 2 else ([0, 1,0] if np.argmax(item) == 1 else [1,0,0]) for item in label]
                label_for_normal = np.array(label_for_normal, dtype=np.float32)
                label_for_tumor = np.array(label_for_tumor, dtype=np.float32)
                yield (normal_matrix, tumor_matrix), (label_for_normal, label_for_tumor)
            else:
                label_for_tumor = [
                    [0, 0, 1] if np.argmax(item) == 2 else ([0, 1, 0] if np.argmax(item) == 1 else [1, 0, 0]) for item
                    in label]
                label_for_tumor = np.array(label_for_tumor, dtype=np.float32)
                yield (normal_matrix, tumor_matrix), (label_for_tumor,)
                # yield (normal_matrix, tumor_matrix), (label[:,:label_shape_cum[0]],)


    train_dataset = tf.data.Dataset.from_generator(
        lambda: DataGenerator(table_dataset_list, train_data_size, train_shuffle_chunk_list, True), TensorDtype,
        TensorShape).prefetch(buffer_size=tf.data.experimental.AUTOTUNE)#.cache().shuffle(buffer_size=param.trainBatchSize * 5, reshuffle_each_iteration=True)
    validate_dataset = tf.data.Dataset.from_generator(
        lambda: DataGenerator(validate_table_dataset_list, validate_data_size, validate_shuffle_chunk_list, False), TensorDtype,
        TensorShape).prefetch(buffer_size=tf.data.experimental.AUTOTUNE)#.cache()

    total_steps = max_epoch * train_data_size // batch_size

    optimizer = tf.optimizers.Adam()
    if task_num == 1:

        # loss_func = [BinaryCrossentropy() for task in range(task_num)]
        loss_func = [FocalLoss(label_shape_cum, task) for task in range(task_num)]

        loss_task = {"output_{}".format(task + 1): loss_func[task] for task in range(task_num)}
        metrics = {"output_{}".format(task + 1): tfa.metrics.F1Score(num_classes=label_shape[task], average='micro') for
                   task in range(task_num)}
    else:
            loss_func = [FocalLoss(label_shape_cum, task) for task in range(task_num)]

            loss_task = {"output_{}".format(task + 1): loss_func[task] for task in range(task_num)}
            metrics = {"output_{}".format(task + 1): tfa.metrics.F1Score(num_classes=label_shape[task], average='micro')
                       for
                       task in range(task_num)}

    model = model_path.Clair3_F(add_indel_length=add_indel_length,two_task=use_siam)

    model.compile(
        loss=loss_task,
        metrics=metrics,
        optimizer=optimizer
    )
    early_stop_callback = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=10, mode="min")
    model_save_callbakck = tf.keras.callbacks.ModelCheckpoint(os.path.join(ochk_prefix, "{epoch}") if ochk_prefix else "{epoch}", period=1, save_weights_only=False)

    # Use first 20 element to initialize tensorflow model using graph mode
    output = model((np.array(table_dataset_list[0].root.normal_matrix[:20]),np.array(table_dataset_list[0].root.tumor_matrix[:20])))
    logging.info(model.summary(print_fn=logging.info))

    logging.info("[INFO] The size of dataset: {}".format(total_data_size))
    logging.info("[INFO] The training batch size: {}".format(batch_size))
    logging.info("[INFO] The training learning_rate: {}".format(learning_rate))
    logging.info("[INFO] Total training steps: {}".format(total_steps))
    logging.info("[INFO] Maximum training epoch: {}".format(max_epoch))
    logging.info("[INFO] Start training...")

    validate_dataset = validate_dataset if add_validation_dataset else None
    if args.chkpnt_fn is not None:
        model.load_weights(args.chkpnt_fn)

    train_history = model.fit(x=train_dataset,
                              epochs=max_epoch,
                              validation_data=validate_dataset,
                              callbacks=[early_stop_callback, model_save_callbakck],
                              verbose=1,
                              shuffle=False)

    for table_dataset in table_dataset_list:
        table_dataset.close()

    for table_dataset in validate_table_dataset_list:
        table_dataset.close()

    # show the parameter set with the smallest validation loss
    if 'val_loss' in train_history.history:
        best_validation_epoch = np.argmin(np.array(train_history.history["val_loss"])) + 1
        logging.info("[INFO] Best validation loss at epoch: %d" % best_validation_epoch)
    else:
        best_train_epoch = np.argmin(np.array(train_history.history["loss"])) + 1
        logging.info("[INFO] Best train loss at epoch: %d" % best_train_epoch)


def main():
    parser = ArgumentParser(description="Train a Clair3 model")

    parser.add_argument('--platform', type=str, default="ont",
                        help="Sequencing platform of the input. Options: 'ont,hifi,ilmn', default: %(default)s")

    parser.add_argument('--bin_fn', type=str, default="", required=True,
                        help="Binary tensor input generated by Tensor2Bin.py, support multiple bin readers using pytables")

    parser.add_argument('--chkpnt_fn', type=str, default=None,
                        help="Input a model to resume training or for fine-tuning")

    parser.add_argument('--ochk_prefix', type=str, default=None,
                        help="Prefix for model output after each epoch")

    # options for advanced users
    parser.add_argument('--maxEpoch', type=int, default=None,
                        help="Maximum number of training epochs")

    parser.add_argument('--learning_rate', type=float, default=None,
                        help="Set the initial learning rate, default: %(default)s")

    parser.add_argument('--validation_dataset', action='store_true',
                        help="Use validation dataset when training, default: %(default)s")

    parser.add_argument('--exclude_training_samples', type=str, default=None,
                        help="Define training samples to be excluded")

    parser.add_argument('--use_siam', action='store_true',
                        help=SUPPRESS)

    parser.add_argument('--add_contrastive', action='store_true',
                        help=SUPPRESS)

    parser.add_argument('--ctgName', type=str, default=None,
                        help="Define training samples to be excluded")
    # Internal process control
    ## In pileup training mode or not
    parser.add_argument('--pileup', action='store_true',
                        help=SUPPRESS)

    ## Add indel length for training and calling, default true for full alignment
    parser.add_argument('--add_indel_length', type=str2bool, default=False,
                        help=SUPPRESS)

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    train_model(args)


if __name__ == "__main__":
    main()
