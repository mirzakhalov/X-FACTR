from typing import Set, List, Tuple, Iterable, Dict
import os
from collections import defaultdict
import argparse
import json
import uuid
import numpy as np
import random
from tqdm import tqdm
import matplotlib.pyplot as plt

SEED = 2020
random.seed(SEED)
np.random.seed(SEED)


def filter_by_relations(filename: str, relations: Set[str]) -> Iterable[Tuple]:
    with open(filename, 'r') as fin:
        for doc in json.load(fin):
            for triple in doc['triples']:
                fact: List[Tuple[str, str]] = []
                for field in ['subject', 'predicate', 'object']:
                    uri = triple[field]['uri'].rsplit('/', 1)[1]
                    label = triple[field]['surfaceform']
                    fact.append((uri, label))
                if not fact[1][0].startswith('P') or \
                        not fact[0][0].startswith('Q') or \
                        not fact[2][0].startswith('Q'):
                    continue
                if fact[1][0] not in relations:
                    continue
                yield fact


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='build multi-entity TREx')
    parser.add_argument('--task', type=str, choices=['gen', 'sample'])
    parser.add_argument('--inp', type=str, help='input file')
    parser.add_argument('--out', type=str, help='output file')
    parser.add_argument('--prop', action='store_true', help='sample proportionally to the frequency')
    args = parser.parse_args()

    if args.task == 'gen':

        with open('data/TREx-relations.jsonl', 'r') as fin:
            relations: Set[str] = set([json.loads(l)['relation'] for l in fin])

        os.makedirs(args.out, exist_ok=True)
        pid2facts: Dict[str, Dict[Tuple[str, str], Tuple[Dict[str, int], Dict[str, int], int]]] = \
            defaultdict(lambda: defaultdict(lambda: [defaultdict(lambda: 0), defaultdict(lambda: 0), 0]))
        for root, dirs, files in os.walk(args.inp):
            print('#files {}'.format(len(files)))
            for file in tqdm(files):
                if not file.endswith('.json'):
                    continue
                for (su, sl), (pu, pl), (ou, ol) in filter_by_relations(os.path.join(root, file), relations):
                    pid2facts[pu][(su, ou)][0][sl] += 1
                    pid2facts[pu][(su, ou)][1][ol] += 1
                    pid2facts[pu][(su, ou)][2] += 1
        for pid, subobj2count in pid2facts.items():
            with open(os.path.join(args.out, pid + '.jsonl'), 'w') as fout:
                for (sub, obj), count in sorted(subobj2count.items(), key=lambda x: -x[1][2]):
                    sl: str = sorted(count[0].items(), key=lambda x: -x[1])[0][0]
                    ol: str = sorted(count[1].items(), key=lambda x: -x[1])[0][0]
                    count: int = count[2]
                    fout.write(json.dumps({
                        'uuid': str(uuid.uuid1()),
                        'predicate_id': pid,
                        'sub_uri': sub,
                        'sub_label': sl,
                        'obj_uri': obj,
                        'obj_label': ol,
                        'count': count
                    }) + '\n')

    elif args.task == 'sample':
        count = 100
        pid2dist: Dict[str, List[float]] = {}
        for root, dirs, files in os.walk(args.inp):
            for file in tqdm(files):
                if not file.endswith('.jsonl'):
                    continue
                with open(os.path.join(root, file), 'r') as fin:
                    probs: List[int] = []
                    facts: List[str] = []
                    min_freq: int = 1e10
                    max_freq: int = 0
                    for l in fin:
                        f = json.loads(l)
                        c = f['count']
                        facts.append(l)
                        probs.append(c)
                        if c < min_freq:
                            min_freq = c
                        if c > max_freq:
                            max_freq = c
                    total = np.sum(probs)
                    probs = np.array(probs) / total
                    choices = sorted(np.random.choice(len(probs), count, replace=False, p=probs if args.prop else None))

                inter = probs[0] - probs[-1]
                dist: List[float] = sorted([(probs[i] - probs[-1]) / (inter or 1) for i in choices])
                pid2dist[file.rsplit('.', 1)[0]] = dist

                if args.out:
                    with open(os.path.join(args.out, file), 'w') as fout:
                        for i in choices:
                            fout.write(facts[i])
            break

        x = []
        y = []
        for i, (k, v) in enumerate(pid2dist.items()):
            x.extend(v)
            y.extend([i / len(pid2dist)] * len(v))
        plt.scatter(x, y)
        plt.ylabel('Relation')
        plt.xlabel('Frequency')
        plt.savefig('dist.png')
