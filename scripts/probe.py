import sys
from os.path import dirname, abspath
sys.path.insert(0, dirname(dirname(dirname(abspath(__file__)))))

from typing import List, Dict, Tuple, Set
import torch
from transformers import *
import json
import numpy as np
import os
from tqdm import tqdm
import argparse
import logging
from collections import defaultdict
import pandas
import csv
from prompt import Prompt
from check_gender import load_entity_gender

logger = logging.getLogger('mLAMA')
logger.setLevel(logging.ERROR)

SUB_LABEL = '##'
PREFIX_DATA = '../LAMA/'
VOCAB_PATH = PREFIX_DATA + 'pre-trained_language_models/common_vocab_cased.txt'
ENTITY_PATH = 'data/TREx/{}.jsonl'
RELATION_PATH = 'data/TREx-relations.jsonl'
PROMPT_LANG_PATH = 'data/TREx_prompts.csv'
ENTITY_LANG_PATH = 'data/TREx_unicode_escape.txt'
ENTITY_GENDER_PATH = 'data/TREx_gender.txt'
LM_NAME = {
    # multilingual model
    'mbert_base': 'bert-base-multilingual-cased',
    'xlmr_base': 'xlm-roberta-base',
    # language-specific model
    'bert_base': 'bert-base-cased',
    'zh_bert_base': 'bert-base-chinese',
    'el_bert_base': 'nlpaueb/bert-base-greek-uncased-v1',
    'fr_roberta_base': 'camembert-base',
    'nl_bert_base': 'bert-base-dutch-cased',
}

def batcher(data: List, batch_size: int):
    for i in range(0, len(data), batch_size):
        yield data[i:i + batch_size]

def load_entity_lang(filename: str) -> Dict[str, Dict[str, str]]:
    entity2lang = defaultdict(lambda: {})
    with open(filename, 'r') as fin:
        for l in fin:
            l = l.strip().split('\t')
            entity = l[0]
            for lang in l[1:]:
                label ,lang = lang.rsplit('@', 1)
                entity2lang[entity][lang] = label.strip('"')
    return entity2lang


def load_word_ids(ids: List[int], tokenizer) -> str:
    tokens: List[Tuple[str, int]] = []
    for t in tokenizer.convert_ids_to_tokens(ids):
        if t == PAD_LABEL:
            continue
        if t.startswith(SUB_LABEL) and len(tokens) > 0:  # TODO: not with RoBERTa
            tokens[-1][0] += t[len(SUB_LABEL):]
            tokens[-1][1] += 1
        else:
            tokens.append([t, 1])
    return ' '.join(map(lambda t: '{}:{}'.format(*t) if t[1] > 1 else t[0], tokens))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='probe LMs with multilingual LAMA')
    parser.add_argument('--probe', type=str, help='probe dataset', choices=['lama', 'lama-uhn'], default='lama')
    parser.add_argument('--model', type=str, help='LM to probe file',
                        choices=['mbert_base', 'xlmr_base',
                                 'bert_base', 'zh_bert_base', 'el_bert_base',
                                 'fr_roberta_base', 'nl_bert_base'], default='mbert_base')
    parser.add_argument('--lang', type=str, help='language to probe',
                        choices=['en', 'zh-cn', 'el', 'fr', 'nl'], default='en')
    parser.add_argument('--prompt_model_lang', type=str, help='prompt model to use',
                        choices=['en', 'el'], default='en')
    parser.add_argument('--portion', type=str, choices=['all', 'trans', 'non'], default='trans',
                        help='which portion of facts to use')
    parser.add_argument('--prompts', type=str, default=None,
                        help='directory where multiple prompts are stored for each relation')
    parser.add_argument('--sub_obj_same_lang', action='store_true',
                        help='use the same language for sub and obj')
    parser.add_argument('--skip_multi_word', action='store_true',
                        help='skip objects with multiple words (not sub-words)')
    parser.add_argument('--facts', type=str, help='file path to facts', default=None)
    parser.add_argument('--disable_inflection', type=str, choices=['x', 'y', 'xy'])
    parser.add_argument('--disable_article', action='store_true')
    parser.add_argument('--log_dir', type=str, help='directory to store prediction results', default=None)
    parser.add_argument('--num_mask', type=int, help='the maximum number of masks to insert', default=5)
    parser.add_argument('--batch_size', type=int, help='the real batch size is this times num_mask', default=4)
    args = parser.parse_args()

    LM = LM_NAME[args.model]
    LANG = args.lang

    # default for BERT-like models
    MASK_LABEL = '[MASK]'
    UNK_LABEL = '[UNK]'
    PAD_LABEL = '[PAD]'
    if LM in {'camembert-base', 'xlmr_base'}:  # RoBERTa
        MASK_LABEL = '<mask>'
        UNK_LABEL = '<unk>'
        PAD_LABEL = '<pad>'

    NUM_MASK = args.num_mask
    BATCH_SIZE = args.batch_size

    if args.probe == 'lama':
        ENTITY_PATH = 'data/TREx/{}.jsonl'
    elif args.probe == 'lama-uhn':
        ENTITY_PATH = 'data/TREx_UHN/{}.jsonl'

    # load relations and templates
    patterns = []
    with open(RELATION_PATH) as fin:
        patterns.extend([json.loads(l) for l in fin])
    entity2lang = load_entity_lang(ENTITY_LANG_PATH)
    entity2gender = load_entity_gender(ENTITY_GENDER_PATH)
    prompt_lang = pandas.read_csv(PROMPT_LANG_PATH)

    # load facts
    restricted_facts = None
    if args.facts is not None:
        filename, part = args.facts.split(':')
        with open(filename, 'r') as fin:
            restricted_facts = set(map(tuple, json.load(fin)[part]))
            print('#restricted facts {}'.format(len(restricted_facts)))

    # load model
    print('load model')
    #tokenizer = AutoTokenizer.from_pretrained(LM)
    tokenizer = BertTokenizer.from_pretrained(LM)
    MASK = tokenizer.convert_tokens_to_ids(MASK_LABEL)
    UNK = tokenizer.convert_tokens_to_ids(UNK_LABEL)
    PAD = tokenizer.convert_tokens_to_ids(PAD_LABEL)
    model = AutoModelWithLMHead.from_pretrained(LM)
    model.to('cuda')
    model.eval()

    # load promp rendering model
    prompt_model = Prompt.from_lang(LANG, args.disable_inflection, args.disable_article)

    # load vocab
    '''
    with open(VOCAB_PATH) as fin:
        allowed_vocab = [l.strip() for l in fin]
        allowed_vocab = set(allowed_vocab)
    # TODO: not work with RoBERTa
    restrict_vocab = [tokenizer.vocab[w] for w in tokenizer.vocab if not w in allowed_vocab]
    '''
    # TODO: add a shared vocab for all LMs?
    restrict_vocab = []

    if args.log_dir and not os.path.exists(args.log_dir):
        os.makedirs(args.log_dir)

    all_queries = []
    for pattern in patterns:
        relation = pattern['relation']
        if args.log_dir:
            log_file = open(os.path.join(args.log_dir, relation + '.csv'), 'w')
            log_file.write('sentence,prediction,gold_inflection,is_same,gold_original,is_same\n')
            log_file_csv = csv.writer(log_file)
        try:
            '''
            if relation != 'P413':
                continue
            '''

            # prepare facts
            f = ENTITY_PATH.format(relation)
            if not os.path.exists(f):
                continue

            queries: List[Dict] = []
            num_skip = not_exist = num_multi_word = 0
            with open(f) as fin:
                for l in fin:
                    l = json.loads(l)
                    sub_exist = LANG in entity2lang[l['sub_uri']]
                    obj_exist = LANG in entity2lang[l['obj_uri']]
                    if restricted_facts is not None and \
                            (l['sub_uri'], l['obj_uri']) not in restricted_facts:
                        continue
                    exist = sub_exist and obj_exist
                    if args.portion == 'trans' and not exist:
                        num_skip += 1
                        continue
                    elif args.portion == 'non' and exist:
                        num_skip += 1
                        continue
                    # load gender
                    l['sub_gender'] = entity2gender[l['sub_uri']]
                    l['obj_gender'] = entity2gender[l['obj_uri']]
                    # resort to English label
                    if args.sub_obj_same_lang:
                        l['sub_label'] = entity2lang[l['sub_uri']][LANG if exist else 'en']
                        l['obj_label'] = entity2lang[l['obj_uri']][LANG if exist else 'en']
                    else:
                        l['sub_label'] = entity2lang[l['sub_uri']][LANG if sub_exist else 'en']
                        l['obj_label'] = entity2lang[l['obj_uri']][LANG if obj_exist else 'en']
                    if UNK in tokenizer.convert_tokens_to_ids(tokenizer.tokenize(l['sub_label'])) or \
                            UNK in tokenizer.convert_tokens_to_ids(tokenizer.tokenize(l['sub_label'])):
                        not_exist += 1
                        continue
                    if args.skip_multi_word and ' ' in l['obj_label']:
                        num_multi_word += 1
                        continue
                    queries.append(l)

            # get prompt
            if args.prompts:
                with open(os.path.join(args.prompts, relation + '.jsonl'), 'r') as fin:
                    prompts = [json.loads(l)['template'] for l in fin][:50]  # TODO: top 50
            else:
                prompts = [prompt_lang[prompt_lang['pid'] == relation][LANG].iloc[0]]

            correct_facts: Set[Tuple[str, str]] = set()
            for prompt in prompts:
                acc, len_acc, acc_ori, len_acc_ori = [], [], [], []
                for query_li in tqdm(batcher(queries, BATCH_SIZE), desc=relation, disable=True):
                    obj_li: List[np.ndarray] = []
                    obj_ori_li: List[np.ndarray] = []
                    inp_tensor: List[torch.Tensor] = []
                    batch_size = len(query_li)
                    for query in query_li:
                        # fill in subject and masks
                        instance_x, _ = prompt_model.fill_x(
                            prompt, query['sub_uri'], query['sub_label'], gender=query['sub_gender'])
                        for nm in range(NUM_MASK):
                            inp, obj_label = prompt_model.fill_y(
                                instance_x, query['obj_uri'], query['obj_label'], gender=query['obj_gender'],
                                num_mask=nm + 1, mask_sym=MASK_LABEL)
                            if args.model == 'el_bert_base':  # TODO: may be unnecessary
                                inp = prompt_model.normalize(inp, mask_sym=MASK_LABEL)
                                obj_label = prompt_model.normalize(obj_label)
                            #print(inp)
                            inp: List[int] = tokenizer.encode(inp)
                            #print(inp)
                            #print(tokenizer.convert_ids_to_tokens(inp))
                            inp_tensor.append(torch.tensor(inp))
                        #input()

                        # tokenize gold object
                        obj = np.array(tokenizer.convert_tokens_to_ids(tokenizer.tokenize(obj_label))).reshape(-1)
                        if len(obj) > NUM_MASK:
                            logger.warning('{} is splitted into {} tokens'.format(obj_label, len(obj)))
                        obj_li.append(obj)
                        # tokenize gold object (before inflection)
                        obj_ori = np.array(tokenizer.convert_tokens_to_ids(tokenizer.tokenize(query['obj_label']))).reshape(-1)
                        obj_ori_li.append(obj_ori)

                    # SHAPE: (batch_size * num_mask, seq_len)
                    inp_tensor: torch.Tensor = torch.nn.utils.rnn.pad_sequence(
                        inp_tensor, batch_first=True, padding_value=PAD).cuda()
                    attention_mask: torch.Tensor = inp_tensor.ne(0).long().cuda()
                    # SHAPE: (batch_size, num_mask, seq_len)
                    mask_ind: torch.Tensor = inp_tensor.eq(MASK).float().cuda().view(batch_size, NUM_MASK, -1)

                    # SHAPE: (batch_size * num_mask, seq_len, vocab_size)
                    logit = model(inp_tensor, attention_mask=attention_mask)[0]
                    logit[:, :, restrict_vocab] = float('-inf')
                    logprob = logit.log_softmax(-1)
                    # SHAPE: (batch_size * num_mask, seq_len)
                    logprob, rank = logprob.max(-1)
                    # SHAPE: (batch_size, num_mask, seq_len)
                    logprob, rank = logprob.view(batch_size, NUM_MASK, -1), rank.view(batch_size, NUM_MASK, -1)

                    # find the best setting
                    inp_tensor = inp_tensor.view(batch_size, NUM_MASK, -1)
                    for i, avg_log in enumerate(((logprob * mask_ind).sum(-1) / mask_ind.sum(-1))):
                        best_num_mask = avg_log.max(0)[1]
                        obj = obj_li[i]
                        obj_ori = obj_ori_li[i]
                        pred: np.ndarray = rank[i, best_num_mask].masked_select(mask_ind[i, best_num_mask].eq(1))\
                            .detach().cpu().numpy().reshape(-1)
                        is_correct = int((len(pred) == len(obj)) and (pred == obj).all())
                        is_correct_ori = int((len(pred) == len(obj_ori)) and (pred == obj_ori).all())
                        acc.append(is_correct)
                        acc_ori.append(is_correct_ori)
                        len_acc.append(int((len(pred) == len(obj))))
                        len_acc_ori.append(int((len(pred) == len(obj_ori))))
                        if is_correct:
                            correct_facts.add((query_li[i]['sub_uri'], query_li[i]['obj_uri']))
                        '''
                        print('===', tokenizer.convert_ids_to_tokens(obj), is_correct, '===')
                        for j in range(NUM_MASK):
                            print(tokenizer.convert_ids_to_tokens(inp_tensor[i, j].detach().cpu().numpy()))
                            tpred = rank[i, j].masked_select(mask_ind[i, j].eq(1)).detach().cpu().numpy().reshape(-1)
                            print(tokenizer.convert_ids_to_tokens(tpred), avg_log[j])
                        input()
                        '''
                        if args.log_dir:
                            log_file_csv.writerow([
                                load_word_ids(inp_tensor[i, best_num_mask].detach().cpu().numpy(), tokenizer),
                                load_word_ids(pred, tokenizer),
                                load_word_ids(obj, tokenizer), is_correct,
                                load_word_ids(obj_ori, tokenizer), is_correct_ori])
                        '''
                        if len(pred) == len(obj):
                            print('pred {}\tgold {}'.format(
                                tokenizer.convert_ids_to_tokens(pred), tokenizer.convert_ids_to_tokens(obj)))
                            input()
                        '''
                print('pid {}\tacc {:.4f}/{:.4f}\tlen_acc {:.4f}/{:.4f}\tprompt {}'.format(
                    relation, np.mean(acc), np.mean(acc_ori), np.mean(len_acc), np.mean(len_acc_ori), prompt))
            print('pid {}\t#fact {}\t#notrans {}\t#notexist {}\t#skipmultiword {}\toracle {:.4f}'.format(
                relation, len(queries), num_skip, not_exist, num_multi_word, len(correct_facts) / len(queries)))
        except Exception as e:
            # TODO: article for 'ART;INDEF;NEUT;PL;ACC' P31
            print('bug for pid {}'.format(relation))
            print(e)
        finally:
            if args.log_dir:
                log_file.close()
