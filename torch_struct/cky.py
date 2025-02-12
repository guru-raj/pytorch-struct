import torch
from .helpers import _Struct
from .semirings import LogSemiring

A, B = 0, 1


class CKY(_Struct):
    def _dp(self, scores, lengths=None, force_grad=False):
        terms, rules, roots = scores
        semiring = self.semiring
        ssize = semiring.size()
        batch, N, T = terms.shape
        _, NT, _, _ = rules.shape
        S = NT + T

        terms, rules, roots = (
            semiring.convert(terms),
            semiring.convert(rules),
            semiring.convert(roots),
        )
        if lengths is None:
            lengths = torch.LongTensor([N] * batch)
        beta = self._make_chart(2, (batch, N, N, NT + T), rules, force_grad)
        span = self._make_chart(N, (batch, N, NT + T), rules, force_grad)
        rule_use = [
            self._make_chart(1, (batch, N - w - 1, NT, S, S), rules, force_grad)[0]
            for w in range(N - 1)
        ]
        top = self._make_chart(1, (batch, NT), rules, force_grad)[0]
        term_use = self._make_chart(1, (batch, N, T), terms, force_grad)[0]
        term_use[:] = terms + 0.0
        beta[A][:, :, :, 0, NT:] = term_use
        beta[B][:, :, :, N - 1, NT:] = term_use
        X_Y_Z = rules.view(ssize, batch, 1, NT, S, S)[:, :, :, :, :NT, :NT]
        X_Y_Z1 = rules.view(ssize, batch, 1, NT, S, S)[:, :, :, :, :NT, NT:]
        X_Y1_Z = rules.view(ssize, batch, 1, NT, S, S)[:, :, :, :, NT:, :NT]
        X_Y1_Z1 = rules.view(ssize, batch, 1, NT, S, S)[:, :, :, :, NT:, NT:]

        # here
        for w in range(1, N):
            Y = beta[A][:, :, : N - w, :w, :NT].view(ssize, batch, N - w, w, 1, NT, 1)
            Z = beta[B][:, :, w:, N - w :, :NT].view(ssize, batch, N - w, w, 1, 1, NT)
            rule_use[w - 1][:, :, :, :, :NT, :NT] = semiring.times(
                semiring.sum(semiring.times(Y, Z), dim=3), X_Y_Z
            )
            Y = beta[A][:, :, : N - w, w - 1, :NT].view(ssize, batch, N - w, 1, NT, 1)
            Z = beta[B][:, :, w:, N - 1, NT:].view(ssize, batch, N - w, 1, 1, T)
            rule_use[w - 1][:, :, :, :, :NT, NT:] = semiring.times(Y, Z, X_Y_Z1)

            Y = beta[A][:, :, : N - w, 0, NT:].view(ssize, batch, N - w, 1, T, 1)
            Z = beta[B][:, :, w:, N - w, :NT].view(ssize, batch, N - w, 1, 1, NT)
            rule_use[w - 1][:, :, :, :, NT:, :NT] = semiring.times(Y, Z, X_Y1_Z)

            if w == 1:
                Y = beta[A][:, :, : N - w, w - 1, NT:].view(
                    ssize, batch, N - w, 1, T, 1
                )
                Z = beta[B][:, :, w:, N - w, NT:].view(ssize, batch, N - w, 1, 1, T)
                rule_use[w - 1][:, :, :, :, NT:, NT:] = semiring.times(Y, Z, X_Y1_Z1)

            rulesmid = rule_use[w - 1].view(ssize, batch, N - w, NT, S * S)
            span[w] = semiring.sum(rulesmid, dim=4)
            beta[A][:, :, : N - w, w, :NT] = span[w]
            beta[B][:, :, w:N, N - w - 1, :NT] = beta[A][:, :, : N - w, w, :NT]

        top[:] = torch.stack(
            [beta[A][:, i, 0, l - 1, :NT] for i, l in enumerate(lengths)], dim=1
        )
        log_Z = semiring.dot(top, roots)
        return semiring.unconvert(log_Z), (term_use, rule_use, top), beta

    def marginals(self, scores, lengths=None, _autograd=True):
        """
        Compute the marginals of a CFG using CKY.

        Parameters:
            terms : b x n x T
            rules : b x NT x (NT+T) x (NT+T)
            root:   b x NT

        Returns:
            v: b tensor of total sum
            spans: bxNxT terms, (bxNxNxNTxSxS) rules, bxNT roots

        """
        terms, rules, roots = scores
        batch, N, T = terms.shape
        _, NT, _, _ = rules.shape
        S = NT + T
        v, (term_use, rule_use, top), alpha = self._dp(
            scores, lengths=lengths, force_grad=True
        )
        if _autograd or self.semiring is not LogSemiring:
            marg = torch.autograd.grad(
                v.sum(dim=0),
                tuple(rule_use) + (top, term_use),
                create_graph=True,
                only_inputs=True,
                allow_unused=False,
            )
            rule_use = marg[:-2]
            rules = torch.zeros(
                batch, N, N, NT, S, S, dtype=scores[1].dtype, device=scores[1].device
            )
            for w in range(len(rule_use)):
                rules[:, w, : N - w - 1] = self.semiring.unconvert(rule_use[w])

            term_marg = self.semiring.unconvert(marg[-1])
            root_marg = self.semiring.unconvert(marg[-2])

            assert term_marg.shape == (batch, N, T)
            assert root_marg.shape == (batch, NT)
            return (term_marg, rules, root_marg)
        else:
            return self._dp_backward(scores, lengths, alpha, v)

    @staticmethod
    def to_parts(spans, extra, lengths=None):
        NT, T = extra

        batch, N, N, S = spans.shape
        assert S == NT + T
        terms = torch.zeros(batch, N, T)
        rules = torch.zeros(batch, NT, S, S)
        roots = torch.zeros(batch, NT)
        for b in range(batch):
            roots[b, :] = spans[b, 0, lengths[b] - 1, :NT]
            terms[b, : lengths[b]] = spans[
                b, torch.arange(lengths[b]), torch.arange(lengths[b]), NT:
            ]
            cover = spans[b].nonzero()
            left = {i: [] for i in range(N)}
            right = {i: [] for i in range(N)}
            for i in range(cover.shape[0]):
                i, j, A = cover[i].tolist()
                left[i].append((A, j, j - i + 1))
                right[j].append((A, i, j - i + 1))
            for i in range(cover.shape[0]):
                i, j, A = cover[i].tolist()
                B = None
                for B_p, k, a_span in left[i]:
                    for C_p, k_2, b_span in right[j]:
                        if k_2 == k + 1 and a_span + b_span == j - i + 1:
                            B, C = B_p, C_p
                            break
                if j > i:
                    assert B is not None, "%s" % ((i, j, left[i], right[j], cover),)
                    rules[b, A, B, C] += 1
        return terms, rules, roots

    @staticmethod
    def from_parts(chart):
        terms, rules, roots = chart
        batch, N, N, NT, S, S = rules.shape
        assert terms.shape[1] == N

        spans = torch.zeros(batch, N, N, S, dtype=rules.dtype, device=rules.device)
        rules = rules.sum(dim=-1).sum(dim=-1)
        for n in range(N):
            spans[:, torch.arange(N - n - 1), torch.arange(n + 1, N), :NT] = rules[
                :, n, torch.arange(N - n - 1)
            ]
        spans[:, torch.arange(N), torch.arange(N), NT:] = terms
        return spans, (NT, S - NT)

    @staticmethod
    def _intermediary(spans):
        batch, N = spans.shape[:2]
        splits = {}
        cover = spans.nonzero()
        left, right = {}, {}
        for k in range(cover.shape[0]):
            b, i, j, A = cover[k].tolist()
            left.setdefault((b, i), [])
            right.setdefault((b, j), [])
            left[b, i].append((A, j, j - i + 1))
            right[b, j].append((A, i, j - i + 1))

        for x in range(cover.shape[0]):
            b, i, j, A = cover[x].tolist()
            if i == j:
                continue
            b_final = None
            c_final = None
            k_final = None
            for B_p, k, a_span in left.get((b, i), []):
                if k > j:
                    continue
                for C_p, k_2, b_span in right.get((b, j), []):
                    if k_2 == k + 1 and a_span + b_span == j - i + 1:
                        k_final = k
                        b_final = B_p
                        c_final = C_p
                        break
                if b_final is not None:
                    break
            assert k_final is not None, "%s %s %s %s" % (b, i, j, spans[b].nonzero())
            splits[(b, i, j)] = k_final, b_final, c_final
        return splits

    @classmethod
    def to_networkx(cls, spans):
        cur = 0
        N = spans.shape[1]
        n_nodes = int(spans.sum().item())
        cover = spans.nonzero().cpu()
        order = torch.argsort(cover[:, 2] - cover[:, 1])
        left = {}
        right = {}
        ordered = cover[order]
        label = ordered[:, 3]
        a = []
        b = []
        topo = [[] for _ in range(N)]
        for n in ordered:
            batch, i, j, _ = n.tolist()
            # G.add_node(cur, label=A)
            if i - j != 0:
                a.append(left[(batch, i)][0])
                a.append(right[(batch, j)][0])
                b.append(cur)
                b.append(cur)
                order = max(left[(batch, i)][1], right[(batch, j)][1]) + 1
            else:
                order = 0
            left[(batch, i)] = (cur, order)
            right[(batch, j)] = (cur, order)
            topo[order].append(cur)
            cur += 1
        indices = left
        return (n_nodes, a, b, label), indices, topo

    ###### Test

    def enumerate(self, scores):
        terms, rules, roots = scores
        semiring = self.semiring
        batch, N, T = terms.shape
        _, NT, _, _ = rules.shape

        def enumerate(x, start, end):
            if start + 1 == end:
                yield (terms[:, start, x - NT], [(start, x - NT)])
            else:
                for w in range(start + 1, end):
                    for y in range(NT) if w != start + 1 else range(NT, NT + T):
                        for z in range(NT) if w != end - 1 else range(NT, NT + T):
                            for m1, y1 in enumerate(y, start, w):
                                for m2, z1 in enumerate(z, w, end):
                                    yield (
                                        semiring.times(
                                            semiring.times(m1, m2), rules[:, x, y, z]
                                        ),
                                        [(x, start, w, end)] + y1 + z1,
                                    )

        ls = []
        for nt in range(NT):
            ls += [semiring.times(s, roots[:, nt]) for s, _ in enumerate(nt, 0, N)]
        return semiring.sum(torch.stack(ls, dim=-1)), None

    @staticmethod
    def _rand():
        batch = torch.randint(2, 5, (1,))
        N = torch.randint(2, 5, (1,))
        NT = torch.randint(2, 5, (1,))
        T = torch.randint(2, 5, (1,))
        terms = torch.rand(batch, N, T)
        rules = torch.rand(batch, NT, (NT + T), (NT + T))
        roots = torch.rand(batch, NT)
        return (terms, rules, roots), (batch.item(), N.item())

    def score(self, potentials, parts):
        terms, rules, roots = potentials
        m_term, m_rule, m_root = parts
        b = m_term.shape[0]
        return (
            m_term.mul(terms).view(b, -1).sum(-1)
            + m_rule.sum(dim=1).sum(dim=1).mul(rules).view(b, -1).sum(-1)
            + m_root.mul(roots).view(b, -1).sum(-1)
        )
