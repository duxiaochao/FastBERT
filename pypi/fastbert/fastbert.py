# coding: utf-8
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random
from .config import *
from .utils import *
from .uer.utils.tokenizer import BertTokenizer
from .uer.utils.vocab import Vocab
from .uer.model_builder import build_model
from .uer.utils.optimizers import AdamW, WarmupLinearSchedule
from .uer.layers.multi_headed_attn import MultiHeadedAttention
from .uer.model_saver import save_model
from .uer.model_loader import load_model


class MiniClassifier(nn.Module):

    def __init__(self, 
                 args,
                 input_size,
                 labels_num):
        super(MiniClassifier, self).__init__()
        self.input_size = input_size
        self.cla_hidden_size = 128
        self.cla_heads_num = 2
        self.labels_num = labels_num
        self.pooling = args.pooling
        self.output_layer_0 = nn.Linear(input_size, self.cla_hidden_size)
        self.self_atten = MultiHeadedAttention(self.cla_hidden_size, self.cla_heads_num, args.dropout)
        self.output_layer_1 = nn.Linear(self.cla_hidden_size, self.cla_hidden_size)
        self.output_layer_2 = nn.Linear(self.cla_hidden_size, labels_num)

    def forward(self, hidden, mask):

        hidden = torch.tanh(self.output_layer_0(hidden))
        hidden = self.self_atten(hidden, hidden, hidden, mask)

        if self.pooling == "mean":
            hidden = torch.mean(hidden, dim=-1)
        elif self.pooling == "max":
            hidden = torch.max(hidden, dim=1)[0]
        elif self.pooling == "last":
            hidden = hidden[:, -1, :]
        else:
            hidden = hidden[:, 0, :]

        output_1 = torch.tanh(self.output_layer_1(hidden))
        logits = self.output_layer_2(output_1)
        return logits


class FastBERT(nn.Module):

    def __init__(self,
                 kernel_name,
                 labels,
                 **kwargs):
        """
        Create FastBERT object.

        args:
            kernel_name - str - the name of kernel model, including:
                'google_bert_base_en', 'google_bert_base_zh', etc.
            labels - list - a list containg all the labels.
            seq_length - int - the sentence length for FastBERT, default 128.
            device - str - 'cpu', 'cuda:0', 'cuda:1', etc.
        """
        super(FastBERT, self).__init__()
        assert kernel_name in MODEL_CONFIG_FILE.keys(), \
                "kernel_name must be in {}".format(MODEL_CONFIG_FILE.keys())

        self.args = load_hyperparam(MODEL_CONFIG_FILE[kernel_name], \
                file_dir=FILES_DIR)
        self.args.seq_length = kwargs.get('seq_length', DEFAULT_SEQ_LENGTH)
        self.args.device = torch.device(kwargs.get('device', DEFAULT_DEVICE))

        assert isinstance(labels, list), "labels must be a list."
        self.label_map = {k: v for v, k in enumerate(labels)}
        self.id2label = {v: k for k, v in self.label_map.items()}
        self.labels_num = len(labels)

        # create vocab
        self.vocab = Vocab()
        self.vocab.load(self.args.vocab_path, is_quiet=True)
        self.args.vocab = self.vocab
        self.cls_id = self.vocab.get('[CLS]')
        self.pad_id = self.vocab.get('[PAD]')

        # create tokenizer
        self.tokenizer = BertTokenizer(self.args)

        # create kernel
        self.args.target = 'bert'
        self.args.subword_type = 'none'
        self.kernel = build_model(self.args)
        check_or_download(
                self.args.pretrained_model_path,
                self.args.pretrained_model_url, 
                self.args.pretrained_model_md5,
                kernel_name,
                self.args.pretrained_model_url_bak)
        self.kernel.load_state_dict(
                torch.load(self.args.pretrained_model_path),strict=False)

        # create teacher and student classifiers
        self.classifiers = nn.ModuleList([
            MiniClassifier(self.args, self.args.hidden_size, self.labels_num)\
            for i in range(self.kernel.encoder.layers_num)
         ])

        # create loss
        self.softmax = nn.LogSoftmax(dim=-1)
        self.criterion = nn.NLLLoss()
        self.soft_criterion = nn.KLDivLoss(reduction='batchmean')

        # others
        self.to(self.args.device)

    def fit(self,
            sentences_train,
            labels_train,
            verbose=True,
            **kwargs):
        """
        Fine-tuning and self-distilling the FastBERT model.

        args:
            sentences_train - list - a list of training sentences.
            labels_train - list - a list of training labels.
            batch_size - int - batch_size for training.
            sentences_dev - list - a list of validation sentences.
            labels_dev - list - a list of validation labels.
            learning_rate - float - learning rate.
            finetuning_epochs_num - int - the epoch number of finetuning.
            distilling_epochs_num - int - the epoch number of distilling.
            report_steps - int - Report the training process every [report_steps] steps.
            warmup - float - the warmup rate for training.
            dev_speed - float - the speed for evaluating in the self-distilling process.
            model_saving_path - str - the path to saving model.
        """
        if verbose:
            print("[FastBERT]: Training FastBERT")

        batch_size = kwargs.get('batch_size', 16)
        sentences_dev = kwargs.get('sentences_dev', [])
        labels_dev = kwargs.get('labels_dev', [])
        learning_rate = kwargs.get('learning_rate', 2e-5)
        finetuning_epochs_num = kwargs.get('finetuning_epochs_num', 5)
        distilling_epochs_num = kwargs.get('distilling_epochs_num', 10)
        report_steps = kwargs.get('report_steps', 100)
        warmup = kwargs.get('warmup', 0.1)
        dev_speed = kwargs.get('dev_speed', 0.5)
        tmp_model_saving_path = os.path.join(TMP_DIR, 'FastBERT_tmp.bin')
        model_saving_path = kwargs.get('model_saving_path', tmp_model_saving_path)

        self._fine_tuning_backbone(
            sentences_train, labels_train, sentences_dev, labels_dev,
            batch_size, learning_rate, finetuning_epochs_num,
            warmup, report_steps, model_saving_path, verbose)

        self.self_distillation(
            sentences_train, batch_size, learning_rate*10, distilling_epochs_num,
            warmup, report_steps, model_saving_path, sentences_dev,
            labels_dev, dev_speed, verbose
        )

        save_model(self, model_saving_path)
        if verbose:
            print("[FastBERT]: Model have been saved at {}".format(model_saving_path))

    def forward(self,
                sentence,
                speed=0.0):
        """
        Predict labels for the input sentence.

        Input:
            sentence - str - the input sentence.
            speed - float - the speed value (0.0~1.0)
        Return:
            label - str/int - the predict label.
            exec_layer_num - int - the number of the executed layers.
        """
        label_id, exec_layer_num = self._fast_infer(sentence, speed)
        label = self.id2label[label_id]
        return label, exec_layer_num

    def load_model(self,
                   model_path):
        """
        Load the model from the specified path.

        Input:
            sentence - str - the path of model file.
        """
        load_model(self, model_path)

    def save_model(self,
                   model_path):
        """
        Saving model to the specified path.

        Input:
            sentence - str - the path of model file.
        """
        save_model(self, model_path)

    def to_device(self,
                  device):
        """
        Change model the CPU or GPU.

        Input:
            device - str - 'cpu', 'cuda:0', 'cuda:1', etc.
        """
        self.args.device = torch.device(device)
        self.to(self.args.device)

    def _fast_infer(self,
                    sentence,
                    speed):
        ids, mask = self._convert_to_id_and_mask(sentence)

        self.eval()
        with torch.no_grad():
            ids = torch.tensor([ids], dtype=torch.int64, device=self.args.device)  # batch_size x seq_length
            mask = torch.tensor([mask], dtype=torch.int64, device=self.args.device)  # batch_size x seq_length

            # embedding layer
            emb = self.kernel.embedding(ids, mask)  # batch_size x seq_length x emb_size
            mask = (mask > 0).unsqueeze(1).repeat(1, self.args.seq_length, 1).\
                    unsqueeze(1)
            mask = (1.0 - mask.float()) * -10000.0  # batch_size x seq_length x seq_length

            # hidden layers
            hidden = emb
            exec_layer_num = self.kernel.encoder.layers_num
            for i in range(self.kernel.encoder.layers_num):
                hidden = self.kernel.encoder.transformer[i](hidden, mask) # batch_size x seq_length x seq_length
                logits = self.classifiers[i](hidden, mask)  # batch_size x labels_num
                probs = F.softmax(logits, dim=1) # batch_size x labels_num
                uncertainty = calc_uncertainty(probs, \
                        labels_num=self.labels_num).item()
                
                if uncertainty < speed:
                    exec_layer_num = i + 1
                    break
                
        label_id = torch.argmax(probs, dim=1).item()
        label = self.id2label[label_id]
        return label, exec_layer_num

    def _forward_for_loss(self,
                          sentences_batch,
                          labels_batch=None):

        self.train()
        ids_batch, masks_batch = [], []
        for sentence in sentences_batch:
            ids, masks = self._convert_to_id_and_mask(sentence)
            ids_batch.append(ids)
            masks_batch.append(masks)
        ids_batch = torch.tensor(ids_batch, dtype=torch.int64, device=self.args.device)  # batch_size x seq_length
        masks_batch = torch.tensor(masks_batch, dtype=torch.int64, device=self.args.device)  # batch_size x seq_length

        # embedding layer
        embs_batch = self.kernel.embedding(ids_batch, masks_batch)  # batch_size x seq_length x emb_size
        masks_batch = (masks_batch > 0).unsqueeze(1).repeat(1, self.args.seq_length, 1).\
                 unsqueeze(1)
        masks_batch = (1.0 - masks_batch.float()) * -10000.0  # batch_size x seq_length x seq_length

        if labels_batch is not None:

            # training backbone of fastbert
            label_ids_batch = [self.label_map[label] for label in labels_batch]
            label_ids_batch = torch.tensor(label_ids_batch, dtype=torch.int64, 
                    device=self.args.device)

            hiddens_batch = embs_batch
            for i in range(self.kernel.encoder.layers_num):
                hiddens_batch = self.kernel.encoder.transformer[i](
                        hiddens_batch, masks_batch)
            logits_batch = self.classifiers[-1](hiddens_batch, masks_batch)
            loss = self.criterion(
                    self.softmax(logits_batch.view(-1, self.labels_num)), 
                    label_ids_batch.view(-1)
                )

            return loss

        else:

            # distilating the student classifiers
            hiddens_batch = embs_batch
            hiddens_batch_list = []
            with torch.no_grad():
                for i in range(self.kernel.encoder.layers_num):
                    hiddens_batch = self.kernel.encoder.transformer[i](
                            hiddens_batch, masks_batch)
                    hiddens_batch_list.append(hiddens_batch)
                teacher_logits = self.classifiers[-1](
                        hiddens_batch_list[-1], masks_batch
                    ).view(-1, self.labels_num)
                teacher_probs = F.softmax(teacher_logits, dim=1)
            
            loss = 0
            for i in range(self.kernel.encoder.layers_num - 1):
                student_logits = self.classifiers[i](
                        hiddens_batch_list[i], masks_batch
                    ).view(-1, self.labels_num)
                loss += self.soft_criterion(
                        self.softmax(student_logits), teacher_probs)
            return loss

    def _convert_to_id_and_mask(self,
                                sentence):
        ids = [self.cls_id] + \
                [self.vocab.get(t) for t in self.tokenizer.tokenize(sentence)]
        mask = [1] * len(ids)
        if len(ids) >= self.args.seq_length:
            ids = ids[ :self.args.seq_length]
            mask = mask[ :self.args.seq_length]
        else:
            pad_num = self.args.seq_length - len(ids)
            ids = ids + [self.pad_id] * pad_num
            mask = mask + [0] * pad_num
        return ids, mask

    def _fine_tuning_backbone(self,
                             sentences_train,
                             labels_train,
                             sentences_dev,
                             labels_dev,
                             batch_size,
                             learning_rate,
                             epochs_num,
                             warmup,
                             report_steps,
                             model_saving_path,
                             verbose=True):

        if verbose:
            print("[FastBERT]: Fine-tuning the backbone for {} epochs using {}.". \
                    format(epochs_num, self.args.device))

        instances_num = len(sentences_train)
        dev_num = len(sentences_dev)
        train_steps = int(instances_num * epochs_num / batch_size) + 1
        steps_num = instances_num // batch_size

        # create optimizer
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'gamma', 'beta']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer \
                    if not any(nd in n for nd in no_decay)], \
                    'weight_decay_rate': 0.01},
            {'params': [p for n, p in param_optimizer \
                    if any(nd in n for nd in no_decay)], \
                    'weight_decay_rate': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=learning_rate, \
                correct_bias=False)
        scheduler = WarmupLinearSchedule(optimizer, \
                warmup_steps=train_steps*warmup, t_total=train_steps)
        
        # fine-tuning
        best_acc = 0.0
        for epoch in range(epochs_num):
            sentences_train, labels_train = shuffle_pairs(
                    sentences_train, labels_train)
            report_loss = 0.
            for step in range(steps_num):
                optimizer.zero_grad()
                sentences_batch = sentences_train[step*batch_size : (step+1)*batch_size]
                labels_batch = labels_train[step*batch_size : (step+1)*batch_size]
                loss = self._forward_for_loss(sentences_batch, labels_batch)

                report_loss += loss.item()
                if (step+1) % report_steps == 0:
                    ave_loss = report_loss / report_steps
                    report_loss = 0.
                    if verbose:
                        print("[FastBERT]: Fine-tuning epoch {}/{}".\
                                format(epoch+1, epochs_num), 
                                "step {}/{}: loss = {:.3f}". \
                                format(step+1, steps_num, ave_loss))

                loss.backward()
                optimizer.step()
                scheduler.step()

            dev_acc, _ = self._evaluate(sentences_dev, labels_dev, speed=0.0) \
                    if dev_num > 0 else 0.0
            train_acc, _ = self._evaluate(sentences_train, labels_train, speed=0.0)
            if verbose:
                print("[FastBERT]: Evaluating at fine-tuning epoch {}/{}".\
                format(epoch+1, epochs_num), 
                ": train_acc = {:.3f}, dev_acc = {:.3f}". \
                format(train_acc, dev_acc))

            if dev_num > 0:
                if dev_acc > best_acc:
                    # saving model
                    if verbose: 
                        print("[FastBERT]: dev_acc ({}) > best_acc ({}),".\
                              format(dev_acc, best_acc), 
                              "saving model to {}.".\
                              format(model_saving_path))
                    save_model(self, model_saving_path)
                    best_acc = dev_acc
            else:
                if train_acc > best_acc:
                    if verbose: 
                        print("[FastBERT]: train_acc ({}) > best_acc ({}),".\
                              format(train_acc, best_acc), 
                              "saving model to {}.".\
                              format(model_saving_path))
                    save_model(self, model_saving_path)
                    best_acc = train_acc

        # loading the best model
        if verbose:
            print("[FastBERT]: Finish fine-tuning. Loading the best model from {}".\
                    format(model_saving_path))
        load_model(self, model_saving_path)

    def _evaluate(self,
                  sentences_batch,
                  labels_batch,
                  speed):
        total_num = len(sentences_batch)
        right_count = 0
        exec_layers = []
        for sent, label in zip(sentences_batch, labels_batch):
            label_pred, el = self._fast_infer(sent, speed=speed)
            exec_layers.append(el)
            if label == label_pred:
                right_count += 1
        acc = right_count / total_num
        ave_exec_layers = np.mean(exec_layers)
        return acc, ave_exec_layers

    def self_distillation(self,
                          sentences_train,
                          batch_size,
                          learning_rate,
                          epochs_num,
                          warmup,
                          report_steps,
                          model_saving_path,
                          sentences_dev=[],
                          labels_dev=[],
                          dev_speed=0.5,
                          verbose=True):
        if verbose:
            print("[FastBERT]: Self-distilling for {} epochs using {}.". \
                    format(epochs_num, self.args.device))

        instances_num = len(sentences_train)
        dev_num = len(sentences_dev)
        train_steps = int(instances_num * epochs_num / batch_size) + 1
        steps_num = instances_num // batch_size 

        # create optimizer
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'gamma', 'beta']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer \
                    if not any(nd in n for nd in no_decay)], \
                    'weight_decay_rate': 0.01},
            {'params': [p for n, p in param_optimizer \
                    if any(nd in n for nd in no_decay)], \
                    'weight_decay_rate': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=learning_rate, \
                correct_bias=False)
        scheduler = WarmupLinearSchedule(optimizer, \
                warmup_steps=train_steps*warmup, t_total=train_steps)

        for epoch in range(epochs_num):
            random.shuffle(sentences_train)
            report_loss = 0.
            for step in range(steps_num):
                optimizer.zero_grad()

                sentences_batch = sentences_train[step*batch_size : (step+1)*batch_size]
                loss = self._forward_for_loss(sentences_batch)

                report_loss += loss.item()
                if (step+1) % report_steps == 0:
                    ave_loss = report_loss / report_steps
                    report_loss = 0.
                    if verbose:
                        print("[FastBERT]: Self-distilling epoch {}/{}".\
                                format(epoch+1, epochs_num), 
                                "step {}/{}: loss = {:.3f}". \
                                format(step+1, steps_num, ave_loss))

                loss.backward()
                optimizer.step()
                scheduler.step()

            dev_acc, ave_layers = self._evaluate(sentences_dev, labels_dev, speed=0.5) \
                    if dev_num > 0 else 0.0
            print("[FastBERT]: Evaluating at self-disilling epoch {}/{}".\
                    format(epoch+1, epochs_num),
                    "dev_acc = {:.3f}, ave_exec_layers = {:.3f}".format(dev_acc, ave_layers))
            save_model(self, model_saving_path)
            print("[FastBERT]: Saving model to {}".format(model_saving_path))
    
    def show(self):
        print("[FastBER]: The configs of model are listed:")
        for k, v in vars(self.args).items():
            print("{}: {}".format(k, v))

