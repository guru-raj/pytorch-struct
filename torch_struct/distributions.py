import torch
from torch.distributions.distribution import Distribution
from torch.distributions.utils import lazy_property
from .linearchain import LinearChain
from .cky import CKY
from .semimarkov import SemiMarkov
from .alignment import Alignment
from .deptree import DepTree, deptree_nonproj, deptree_part
from .cky_crf import CKY_CRF
from .semirings import (
    LogSemiring,
    MaxSemiring,
    EntropySemiring,
    MultiSampledSemiring,
    KMaxSemiring,
)


class StructDistribution(Distribution):
    r"""
    Base structured distribution class.

    Dynamic distribution for length N of structures :math:`p(z)`.

    Implemented based on gradient identities from:

    * Inside-outside and forward-backward algorithms are just backprop :cite:`eisner2016inside`
    * Semiring Parsing :cite:`goodman1999semiring`
    * First-and second-order expectation semirings with applications to minimum-risk training on translation forests :cite:`li2009first`

    Parameters:
        log_potentials (tensor, batch_shape x event_shape) :  log-potentials :math:`\phi`
        lengths (long tensor, batch_shape) : integers for length masking
    """

    has_enumerate_support = True

    def __init__(self, log_potentials, lengths=None):
        batch_shape = log_potentials.shape[:1]
        event_shape = log_potentials.shape[1:]
        self.log_potentials = log_potentials
        self.lengths = lengths
        super().__init__(batch_shape=batch_shape, event_shape=event_shape)

    def _new(self, *args, **kwargs):
        return self._param.new(*args, **kwargs)

    def log_prob(self, value):
        """
        Compute log probability over values :math:`p(z)`.

        Parameters:
            value (tensor): One-hot events (*sample_shape x batch_shape x event_shape*)

        Returns:
            log_probs (*sample_shape x batch_shape*)
        """

        d = value.dim()
        batch_dims = range(d - len(self.event_shape))
        v = self.struct().score(
            self.log_potentials,
            value.type_as(self.log_potentials),
            batch_dims=batch_dims,
        )
        return v - self.partition

    @lazy_property
    def entropy(self):
        """
        Compute entropy for distribution :math:`H[z]`.

        Returns:
            entropy (*batch_shape*)
        """
        return self.struct(EntropySemiring).sum(self.log_potentials, self.lengths)

    @lazy_property
    def argmax(self):
        r"""
        Compute an argmax for distribution :math:`\arg\max p(z)`.

        Returns:
            argmax (*batch_shape x event_shape*)
        """
        return self.struct(MaxSemiring).marginals(self.log_potentials, self.lengths)

    def topk(self, k):
        r"""
        Compute the k-max for distribution :math:`k\max p(z)`.

        Returns:
            kmax (*k x batch_shape x event_shape*)
        """
        return self.struct(KMaxSemiring(k)).marginals(
            self.log_potentials, self.lengths, _raw=True
        )

    @lazy_property
    def mode(self):
        return self.argmax

    @lazy_property
    def marginals(self):
        """
        Compute marginals for distribution :math:`p(z_t)`.

        Can be used in higher-order calculations, i.e.

        *

        Returns:
            marginals (*batch_shape x event_shape*)
        """
        return self.struct(LogSemiring).marginals(self.log_potentials, self.lengths)

    # @constraints.dependent_property
    # def support(self):
    #     pass

    # @property
    # def param_shape(self):
    #     return self._param.size()

    @lazy_property
    def partition(self):
        "Compute the partition function."
        return self.struct(LogSemiring).sum(self.log_potentials, self.lengths)

    def sample(self, sample_shape=torch.Size()):
        r"""
        Compute structured samples from the distribution :math:`z \sim p(z)`.

        Parameters:
            sample_shape (int): number of samples

        Returns:
            samples (*sample_shape x batch_shape x event_shape*)
        """
        assert len(sample_shape) == 1
        nsamples = sample_shape[0]
        samples = []
        for k in range(nsamples):
            if k % 10 == 0:
                sample = self.struct(MultiSampledSemiring).marginals(
                    self.log_potentials, lengths=self.lengths
                )
                sample = sample.detach()
            tmp_sample = MultiSampledSemiring.to_discrete(sample, (k % 10) + 1)
            samples.append(tmp_sample)
        return torch.stack(samples)

    def to_event(self, sequence, extra, lengths=None):
        "Convert simple representation to event."
        return self.struct.to_parts(sequence, extra, lengths=None)

    def from_event(self, event):
        "Convert event to simple representation."
        return self.struct.from_parts(event)

    def enumerate_support(self, expand=True):
        """
        Compute the full exponential enumeration set.

        Returns:
            (enum, enum_lengths) - (*tuple cardinality x batch_shape x event_shape*)
        """
        _, _, edges, enum_lengths = self.struct().enumerate(
            self.log_potentials, self.lengths
        )
        # if expand:
        #     edges = edges.unsqueeze(1).expand(edges.shape[:1] + self.batch_shape[:1] + edges.shape[1:])
        return edges, enum_lengths


class LinearChainCRF(StructDistribution):
    r"""
    Represents structured linear-chain CRFs with C classes.

    For reference see:

    * An introduction to conditional random fields :cite:`sutton2012introduction`

    Example application:

    * Bidirectional LSTM-CRF Models for Sequence Tagging :cite:`huang2015bidirectional`


    Event shape is of the form:

    Parameters:
        log_potentials (tensor) : event shape (*(N-1) x C x C*) e.g.
                                  :math:`\phi(n,  z_{n+1}, z_{n})`
        lengths (long tensor) : batch_shape integers for length masking.


    Compact representation: N long tensor in [0, ..., C-1]

    Implementation uses linear-scan, forward-pass only.

    * Parallel Time: :math:`O(\log(N))` parallel merges.
    * Forward Memory: :math:`O(N \log(N) C^2)`

    """

    struct = LinearChain


class AlignmentCRF(StructDistribution):
    r"""
    Represents basic alignment algorithm, i.e. dynamic-time warping or Needleman-Wunsch.

    Event shape is of the form:

    Parameters:
        log_potentials (tensor) : event_shape (*N x M x 3*), e.g.
                                    :math:`\phi(i, j, op)`
                                  Ops are 0 -> j-1, 1->i-1,j-1, and 2->i-1
        lengths (long tensor) : batch shape integers for length masking.


    Implementation uses linear-scan.

    * Parallel Time: :math:`O(\log (M +N))` parallel merges.
    * Forward Memory: :math:`O((M+N)^3)`

    """
    struct = Alignment


class HMM(StructDistribution):
    r"""
    Represents hidden-markov smoothing with C hidden states.

    Event shape is of the form:

    Parameters:
        transition (tensor): log-probabilities (*C X C*) :math:`p(z_n| z_n-1)`
        emission (tensor): log-probabilities (*V x C*)  :math:`p(x_n| z_n)`
        init (tensor): log-probabilities (*C*) :math:`p(z_1)`
        observations (long tensor): indices (*batch x N*) between [0, V-1]

    Compact representation: N long tensor in [0, ..., C-1]

    Implemented as a special case of linear chain CRF.
    """

    def __init__(self, transition, emission, init, observations, lengths=None):
        log_potentials = HMM.struct.hmm(transition, emission, init, observations)
        super().__init__(log_potentials, lengths)

    struct = LinearChain


class SemiMarkovCRF(StructDistribution):
    r"""
    Represents a semi-markov or segmental CRF with C classes of max width K

    Event shape is of the form:

    Parameters:
       log_potentials : event shape (*N x K x C x C*) e.g.
                        :math:`\phi(n, k, z_{n+1}, z_{n})`
       lengths (long tensor) : batch shape integers for length masking.

    Compact representation: N long tensor in [-1, 0, ..., C-1]

    Implementation uses linear-scan, forward-pass only.

    * Parallel Time: :math:`O(\log(N))` parallel merges.
    * Forward Memory: :math:`O(N \log(N) C^2 K^2)`

    """

    struct = SemiMarkov


class DependencyCRF(StructDistribution):
    r"""
    Represents a projective dependency CRF.

    Reference:

    * Bilexical grammars and their cubic-time parsing algorithms :cite:`eisner2000bilexical`

    Event shape is of the form:

    Parameters:
       log_potentials (tensor) : event shape (*N x N*) head, child  with
                                 arc scores with root scores on diagonal e.g.
                                 :math:`\phi(i, j)` where :math:`\phi(i, i)` is (root, i).
       lengths (long tensor) : batch shape integers for length masking.


    Compact representation: N long tensor in [0, .. N] (indexing is +1)

    Implementation uses linear-scan, forward-pass only.

    * Parallel Time: :math:`O(N)` parallel merges.
    * Forward Memory: :math:`O(N \log(N) C^2 K^2)`

    """

    struct = DepTree


class TreeCRF(StructDistribution):
    r"""
    Represents a 0th-order span parser with NT nonterminals. Implemented using a
    fast CKY algorithm.

    For example usage see:

    * A Minimal Span-Based Neural Constituency Parser :cite:`stern2017minimal`

    Event shape is of the form:

    Parameters:
        log_potentials (tensor) : event_shape (*N x N x NT*), e.g.
                                    :math:`\phi(i, j, nt)`
        lengths (long tensor) : batch shape integers for length masking.

    Implementation uses width-batched, forward-pass only

    * Parallel Time: :math:`O(N)` parallel merges.
    * Forward Memory: :math:`O(N^2)`

    Compact representation:  *N x N x NT* long tensor (Same)
    """
    struct = CKY_CRF


class SentCFG(StructDistribution):
    """
    Represents a full generative context-free grammar with
    non-terminals NT and terminals T.

    Event shape is of the form:

    Parameters:
        log_potentials (tuple) : event tuple with event shapes
                         terms (*N x T*)
                         rules (*NT x (NT+T) x (NT+T)*)
                         root  (*NT*)
        lengths (long tensor) : batch shape integers for length masking.

    Implementation uses width-batched, forward-pass only

    * Parallel Time: :math:`O(N)` parallel merges.
    * Forward Memory: :math:`O(N^2 (NT+T))`

    Compact representation:  (*N x N x NT*) long tensor
    """

    struct = CKY

    def __init__(self, log_potentials, lengths=None):
        batch_shape = log_potentials[0].shape[:1]
        event_shape = log_potentials[0].shape[1:]
        self.log_potentials = log_potentials
        self.lengths = lengths
        super(StructDistribution, self).__init__(
            batch_shape=batch_shape, event_shape=event_shape
        )


class NonProjectiveDependencyCRF(StructDistribution):
    r"""
    Represents a non-projective dependency CRF.

    For references see:

    * Non-projective dependency parsing using spanning tree algorithms :cite:`mcdonald2005non`
    * Structured prediction models via the matrix-tree theorem :cite:`koo2007structured`

    Event shape is of the form:

    Parameters:
       log_potentials (tensor) : event shape (*N x N*) head, child  with
                                 arc scores with root scores on diagonal e.g.
                                 :math:`\phi(i, j)` where :math:`\phi(i, i)` is (root, i).

    Compact representation: N long tensor in [0, .. N] (indexing is +1)

    Note: Does not currently implement argmax (Chiu-Liu) or sampling.

    """

    struct = DepTree

    @lazy_property
    def marginals(self):
        """
        Compute marginals for distribution :math:`p(z_t)`.

        Algorithm is :math:`O(N^3)` but very fast on batched GPU.

        Returns:
            marginals (*batch_shape x event_shape*)
        """
        return deptree_nonproj(self.log_potentials)

    def sample(self, sample_shape=torch.Size()):
        raise NotImplementedError()

    @lazy_property
    def partition(self):
        """
        Compute the partition function.
        """
        return deptree_part(self.log_potentials)

    @lazy_property
    def argmax(self):
        """
        Use Chiu-Liu Algorithm. :math:`O(N^2)`

        (Currently not implemented)
        """
        raise NotImplementedError()

    @lazy_property
    def entropy(self):
        raise NotImplementedError()
