import os
import random
import re
from collections import OrderedDict
from functools import wraps
from abc import ABC, abstractmethod
import pickle
from razdel import tokenize, sentenize

import torch
import ufal.udpipe

wdir = os.path.dirname(os.path.abspath(__file__))


_SPACES_FIX_PATTERN = re.compile('\s+')


def singleton(cls):
    instance = None

    @wraps(cls)
    def inner(*args, **kwargs):
        nonlocal instance
        if instance is None:
            instance = cls(*args, **kwargs)
        return instance

    return inner


@singleton
class ToktokTokenizer:
    def tokenize(self, text):
        return [_.text for _ in tokenize(text)]

    def sentenize(self, text):
        return [_.text for _ in sentenize(text)]

class AbstractSolver(ABC):
    def __init__(self, seed=42):
        self.seed = seed
        self._init_seed()

    def _init_seed(self):
        random.seed(self.seed)

    def fit(self, tasks):
        pass

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            temp = pickle.load(f)
        assert isinstance(temp, cls)
        return temp

    def save(self, path):
        with open(path, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @abstractmethod
    def predict_from_model(self, task):
        pass


def init_bert(bert_path):
    from pytorch_pretrained_bert import BertModel, BertConfig

    bert_config = BertConfig.from_json_file(os.path.join(bert_path, 'bert_config.json'))
    return BertModel(bert_config)


def init_masked_bert(bert_path):
    from pytorch_pretrained_bert import BertForMaskedLM, BertConfig

    bert_config = BertConfig.from_json_file(os.path.join(bert_path, 'bert_config.json'))
    return BertForMaskedLM(bert_config)


def load_bert(bert_path):
    bert_model = init_bert(bert_path)

    state_dict = torch.load(os.path.join(bert_path, 'pytorch_model.bin'))
    new_state_dict = OrderedDict()
    for key, tensor in state_dict.items():
        if key.startswith('bert'):
            new_state_dict[key[5:]] = tensor
        else:
            new_state_dict[key] = tensor
    missing_keys, unexpected_keys = bert_model.load_state_dict(new_state_dict, strict=False)

    for key in missing_keys:
        print('Key {} is missing in the bert checkpoint!'.format(key))
    for key in unexpected_keys:
        print('Key {} is unexpected in the bert checkpoint!'.format(key))

    bert_model.eval()
    return bert_model

def load_masked_bert(bert_path):
    bert_model = init_masked_bert(bert_path)

    state_dict = torch.load(os.path.join(bert_path, 'pytorch_model.bin'))
    missing_keys, unexpected_keys = bert_model.load_state_dict(state_dict, strict=False)

    for key in missing_keys:
        print('Key {} is missing in the bert checkpoint!'.format(key))
    for key in unexpected_keys:
        print('Key {} is unexpected in the bert checkpoint!'.format(key))

    bert_model.eval()
    return bert_model


def load_bert_tokenizer(bert_path):
    from pytorch_pretrained_bert import BertTokenizer

    return BertTokenizer.from_pretrained(os.path.join(bert_path, 'vocab.txt'), do_lower_case=False)


class BertEmbedder(object):
    """
    Embedding Wrapper on Bert Multilingual Cased
    """

    def __init__(self):
        self.model_file = "data/"
        self.vocab_file = "data/vocab.txt"
        self.model = self.bert_model()
        self.masked_model = self.masked_bert_model()
        self.tokenizer = self.bert_tokenizer()
        self.embedding_matrix = self.get_bert_embed_matrix()

    @singleton
    def bert_model(self):
        model = load_bert(self.model_file)
        return model

    @singleton
    def masked_bert_model(self):
        model = load_masked_bert(self.model_file)
        return model


    @singleton
    def bert_tokenizer(self):
        tokenizer = load_bert_tokenizer(self.model_file)
        return tokenizer

    @singleton
    def get_bert_embed_matrix(self):
        bert_embeddings = list(self.model.children())[0]
        bert_word_embeddings = list(bert_embeddings.children())[0]
        matrix = bert_word_embeddings.weight.data.numpy()
        return matrix

    def sentence_embedding(self, text_list):
        embeddings = []
        for text in text_list:
            token_list = self.tokenizer.tokenize("[CLS] " + text + " [SEP]")
            segments_ids, indexed_tokens = [1] * len(token_list), self.tokenizer.convert_tokens_to_ids(token_list)
            segments_tensors, tokens_tensor = torch.tensor([segments_ids]), torch.tensor([indexed_tokens])
            with torch.no_grad():
                encoded_layers, _ = self.model(tokens_tensor, segments_tensors)
            sent_embedding = torch.mean(encoded_layers[11], 1)
            embeddings.append(sent_embedding)
        return embeddings

    def fill_mask(self, text_before, text_after, token):
        MASK = self.tokenizer.convert_tokens_to_ids(['[MASK]'])[0]
        token = self.tokenizer.tokenize(token)
        token = self.tokenizer.convert_tokens_to_ids(token)
        text = text_before + " [MASK] " * len(token) + text_after
        token_list = self.tokenizer.tokenize("[CLS] " + text + " [SEP]")
        segments_ids, indexed_tokens = [1] * len(token_list), self.tokenizer.convert_tokens_to_ids(token_list)
        segments_tensors, tokens_tensor = torch.tensor([segments_ids]), torch.tensor([indexed_tokens])
        with torch.no_grad():
            logits = self.masked_model(tokens_tensor, segments_tensors)
            p = 0
            mask_idx = indexed_tokens.index(MASK)
            for i in range(len(token)):
                p += logits[0][mask_idx + i][token[i]]
            return p / len(token)

    def contextual_word_embedding(self, text_list):
        embeddings = []
        for text in text_list:
            token_list = self.tokenizer.tokenize("[CLS] " + text + " [SEP]")
            begin = token_list.index("|")
            end = token_list[begin + 1:].index("|") + begin
            token_list.pop(begin)
            token_list.pop(end)
            segments_ids, indexed_tokens = [1] * len(token_list), self.tokenizer.convert_tokens_to_ids(token_list)
            segments_tensors, tokens_tensor = torch.tensor([segments_ids]), torch.tensor([indexed_tokens])
            with torch.no_grad():
                encoded_layers, _ = self.model(tokens_tensor, segments_tensors)
            embeddings.append(torch.mean(encoded_layers[11][0][begin:end], 0))
        return embeddings

    def token_embedding(self, token_list):
        token_embedding = []
        for token in token_list:
            ontoken = self.tokenizer.tokenize(token)
            segments_ids, indexed_tokens = [1] * len(ontoken), self.tokenizer.convert_tokens_to_ids(ontoken)
            segments_tensors, tokens_tensor = torch.tensor([segments_ids]), torch.tensor([indexed_tokens])
            with torch.no_grad():
                encoded_layers, _ = self.model(tokens_tensor, segments_tensors)
            ontoken_embeddings = []
            for subtoken_i in range(len(ontoken)):
                hidden_layers = []
                for layer_i in range(len(encoded_layers)):
                    vector = encoded_layers[layer_i][0][subtoken_i]
                    hidden_layers.append(vector)
                ontoken_embeddings.append(hidden_layers)
            cat_last_4_layers = [torch.cat((layer[-4:]), 0) for layer in ontoken_embeddings]
            token_embedding.append(cat_last_4_layers)
        token_embedding = torch.stack(token_embedding[0], 0) if len(token_embedding) > 1 else token_embedding[0][0]
        return token_embedding


class UDPipeError(Exception):
    def __init__(self, err):
        self.err = err

    def __str__(self):
        return self.err


def iter_words(sentences):
    for s in sentences:
        for w in s.words[1:]:
            yield w


class Pipeline(object):
    def __init__(self, input_format='conllu', model=None, output_format=None, output_stream=None,
                 tag=False, parse=True):
        self.model = model

        # if model:
        #    self.input_format = model.newTokenizer(model.DEFAULT)
        # else:
        self.input_format = ufal.udpipe.InputFormat.newInputFormat(input_format)

        self.pipes = []

        self.pipes.append(self.read_input)

        if tag:
            self.pipes.append(self.tag)
        if parse:
            self.pipes.append(self.parse)
        if output_format:
            self.output_format = ufal.udpipe.OutputFormat.newOutputFormat(output_format)
            self.output_stream = output_stream
            self.pipes.append(self.write_output)

    def read_input(self, data):
        # Input text
        self.input_format.setText(data)

        # Errors will show up here
        error = ufal.udpipe.ProcessingError()

        # Create empty sentence
        sentence = ufal.udpipe.Sentence()

        # Fill sentence object
        while self.input_format.nextSentence(sentence, error):
            # Check for error
            if error.occurred():
                raise UDPipeError(error.message)

            yield sentence

            sentence = ufal.udpipe.Sentence()

    def tag(self, sentences):
        """Tag sentences adding lemmas, pos tags and features for each token."""

        for sentence in sentences:
            self.model.tag(sentence, self.model.DEFAULT)
            yield sentence

    def parse(self, sentences):
        """Tag sentences adding lemmas, pos tags and features for each token."""

        for sentence in sentences:
            self.model.parse(sentence, self.model.DEFAULT)
            yield sentence

    def write_output(self, sentences):
        output = ""

        for sentence in sentences:
            output += self.output_format.writeSentence(sentence)

        output += self.output_format.finishDocument()

        return output

    def process(self, inputs):
        for fn in self.pipes:
            inputs = fn(inputs)

        return inputs


def standardize_task(task):
    if "choices" not in task:
        if "question" in task and "choices" in task["question"]:
            task["choices"] = task["question"]["choices"]
        else:
            parts = task["text"].split("\n")
            task["text"] = parts[0]
            task["choices"] = []
            for i in range(1, len(parts)):
                task["choices"].append({"id": str(i), "text": parts[i]})
    for i in range(len(task["choices"])):
        parts = [x.strip() for x in task["choices"][i]["text"].split(",")]
        task["choices"][i]["parts"] = parts
    return task


def check_solution(task, solution):
    if "correct_variants" in task["solution"]:
        correct = set(task["solution"]["correct_variants"][0])
    elif "correct" in task["solution"]:
        correct = set(task["solution"]["correct"])
    else:
        raise ValueError("Unknown task format!")
    return float(set([str(x) for x in solution]) == correct)


def random_solve_task(task):
    """
    :param task: standardized task
    :return: list of string labels
    """
    choice_decisions = []
    for ch in task["choices"]:
        if random.randint(0, 1):
            choice_decisions.append(ch["id"])
    return choice_decisions


def fix_spaces(text):
    return _SPACES_FIX_PATTERN.sub(' ', text)
