from coq.glob_constr import *
from coq.glob_constr_parser import GlobConstrParser


FILE = "theorems"


class DiffAst(object):
    def __init__(self):
        self.pos = 0
        self.found = False

    def diff_ast(self, c1, c2):
        self.pos = 0
        self.found = False
        self._diff_ast(c1, c2)
        if not self.found:
            assert False
        return self.pos

    def _diff_ast(self, c1, c2):
        if isinstance(c1, GRef) and isinstance(c2, GRef):
            if not self.found:
                self.pos += 1
        elif isinstance(c1, GVar) and isinstance(c2, GVar):
            if not self.found:
                self.pos += 1
        elif isinstance(c1, GApp) and isinstance(c2, GApp):
            if not self.found:
                self.pos += 1
                self._diff_ast(c1.g, c2.g)
                for c1_p, c2_p in zip(c1.gs, c2.gs):
                    if not self.found:
                        self._diff_ast(c1_p, c2_p)
        else:
            self.found = True
            return self.pos


def get_lemmas(lemma_ids):
    # foobar = ["rewrite_eq_{}".format(lemid) for lemid in lemma_ids]

    lemmas = {}
    with open("{}.v".format(FILE), 'r') as f:
        for line in f:
            line = line.strip()
            if "Lemma rewrite_eq" in line:
                idx = line.find(':')
                lemma2 = line[6:idx]
                for lemid in lemma_ids:
                    if "rewrite_eq_{}".format(lemid) == lemma2:
                        lemmas[lemid] = line
                        break
    print("LEMMAS", lemma_ids.difference(set(lemmas.keys())))
    return lemmas


def to_goalattn_dataset(poseval_dataset):
    def clean2(orig):
        dataset = []
        positions = [0 for _ in range(40)]
        for tactr_id, pt in orig:
            # Item 3 contains [TacEdge]
            tac = pt.tacst[3][-1]
            if tac.name.startswith("surgery"):
                args = tac.ftac.tac_args
                rw_dir = GlobConstrParser().parse_glob_constr(args[0])
                orig_ast = GlobConstrParser().parse_glob_constr(args[1])
                rw_ast = GlobConstrParser().parse_glob_constr(args[2])
                pos = DiffAst().diff_ast(orig_ast, rw_ast)
                # print("DIFF", pos, orig_ast, rw_ast)
                # Put the tactic in tac_bin
                # Put the position of the ast in the subtr_bin
                if "{}.id_r".format(FILE) in tac.ftac.gids:
                    pt.tac_bin = 0
                    pt.subtr_bin = 2 * pos
                    positions[2 * pos] += 1
                    dataset += [(tactr_id, pt)]
                elif "{}.id_l".format(FILE) in tac.ftac.gids:
                    pt.tac_bin = 1
                    pt.subtr_bin = 2 * pos + 1
                    positions[2 * pos + 1] += 1
                    dataset += [(tactr_id, pt)]
                else:
                    assert False
        print(positions)
        return dataset

    train = clean2(poseval_dataset.train)
    test = clean2(poseval_dataset.test)
    seen = set()
    for tactr_id, pt in test:
        seen.add(tactr_id)
    print("TEST", len(seen), seen)
    test_lemmas = get_lemmas(seen)
    val = clean2(poseval_dataset.val)
    seen = set()
    for tactr_id, pt in val:
        seen.add(tactr_id)
    print("VALID", len(seen), seen)
    val_lemmas = get_lemmas(seen)
    print("LEN", len(val_lemmas))
    return Dataset(train, test, val), test_lemmas, val_lemmas
