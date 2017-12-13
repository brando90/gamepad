"""
[Note]

Convert "tokens" in dataset into unique integers for embeddings.
"""

class EmbedTokens(object):
    """
    Collect all tokens in the data-set.
    """
    def __init__(self):
        self.unique_sort = set()
        self.unique_const = set()
        self.unique_ind = set()
        self.unique_conid = set()
        self.unique_evar = set()
        self.unique_fix = set()

    def _tokens_to_idx(self, unique):
        ls = list(unique)
        tok_to_idx = {}
        for idx, tok in enumerate(ls):
            tok_to_idx[tok] = idx
        return tok_to_idx

    def tokens_to_idx(self):
        sort_to_idx = self._tokens_to_idx(self.unique_sort)
        const_to_idx = self._tokens_to_idx(self.unique_const)
        ind_to_idx = self._tokens_to_idx(self.unique_ind)
        conid_to_idx = self._tokens_to_idx(self.unique_conid)
        evar_to_idx = self._tokens_to_idx(self.unique_evar)
        fix_to_idx = self._tokens_to_idx(self.unique_fix)

        return (sort_to_idx, const_to_idx, ind_to_idx,
                conid_to_idx, evar_to_idx, fix_to_idx)

    def tokenize_tactr(self, tactr):
        tactr.hist_coqexp()
        self.unique_sort = self.unique_sort.union(tactr.unique_sort())
        self.unique_const = self.unique_const.union(tactr.unique_const())
        self.unique_ind = self.unique_ind.union(tactr.unique_ind())
        self.unique_conid = self.unique_conid.union(tactr.unique_conid())
        self.unique_evar = self.unique_evar.union(tactr.unique_evar())
        self.unique_fix = self.unique_fix.union(tactr.unique_fix())

    def tokenize_tactrs(self, tactrs):
        for tactr in tactrs:
            self.tokenize_tactr(tactr)
