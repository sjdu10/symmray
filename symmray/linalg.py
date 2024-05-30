import functools

import autoray as ar

from .block_core import BlockIndex
from .interface import tensordot


def norm(x):
    xx = tensordot(x.conj(), x, x.ndim)
    xx, = xx.blocks.values()
    return ar.do("sqrt", ar.do("abs", xx))


def _get_qr_fn(backend, stabilized=False):
    _qr = ar.get_lib_fn(backend, "linalg.qr")

    if not stabilized:
        return _qr

    try:
        _qr_stab = ar.get_lib_fn(backend, "qr_stabilized")

        def _qr(x):
            q, _, r = _qr_stab(x)
            return q, r

    except ImportError:
        _qr_ubstab = _qr
        _diag = ar.get_lib_fn(backend, "diag")
        _reshape = ar.get_lib_fn(backend, "reshape")
        _sign = ar.get_lib_fn(backend, "sign")

        def _qr(x):
            q, r = _qr_ubstab(x)
            s = _sign(_diag(r))
            q = q * _reshape(s, (1, -1))
            r = r * _reshape(s, (-1, 1))
            return q, r

    return _qr


def qr(x, stabilized=False):
    if x.ndim != 2:
        raise NotImplementedError(
            "qr only implemented for 2D BlockArrays,"
            f" got {x.ndim}D. Consider fusing first."
        )

    # get the 'lower' qr function that acts on the blocks
    _qr = _get_qr_fn(x.backend, stabilized=stabilized)

    q_blocks = {}
    r_blocks = {}
    new_chargemap = {}

    for sector, array in x.blocks.items():
        q, r = _qr(array)
        q_blocks[sector] = q
        r_blocks[sector] = r
        new_chargemap[sector[1]] = ar.shape(q)[1]

    bond_index = BlockIndex(chargemap=new_chargemap, flow=x.indices[1].flow)
    q = x.__class__(
        indices=(x.indices[0], bond_index),
        charge_total=x.charge_total,
        blocks=q_blocks,
    )
    r = x.__class__(
        indices=(bond_index.conj(), x.indices[1]),
        charge_total=x.symmetry.combine(),
        blocks=r_blocks,
    )
    return q, r


def qr_stabilized(x):
    q, r = qr(x, stabilized=True)
    return q, None, r


def svd(x):
    if x.ndim != 2:
        raise NotImplementedError(
            "svd only implemented for 2D BlockArrays,"
            f" got {x.ndim}D. Consider fusing first."
        )

    _svd = ar.get_lib_fn(x.backend, "linalg.svd")

    u_blocks = {}
    s_store = {}
    v_blocks = {}
    new_chargemap = {}

    for sector, array in x.blocks.items():
        u, s, v = _svd(array)
        u_blocks[sector] = u
        s_store[sector] = s
        v_blocks[sector] = v
        new_chargemap[sector[1]] = ar.shape(u)[1]

    bond_index = BlockIndex(chargemap=new_chargemap, flow=x.indices[1].flow)
    u = x.__class__(
        indices=(x.indices[0], bond_index),
        charge_total=x.charge_total,
        blocks=u_blocks,
    )
    v = x.__class__(
        indices=(bond_index.conj(), x.indices[1]),
        charge_total=x.symmetry.combine(),
        blocks=v_blocks,
    )
    return u, s_store, v


def argsort(seq):
    return sorted(range(len(seq)), key=seq.__getitem__)


@functools.lru_cache(maxsize=2**14)
def calc_sub_max_bonds(sizes, max_bond):
    # overall fraction of the total bond dimension to use
    frac = max_bond / sum(sizes)

    if frac >= 1.0:
        # keep all singular values
        return sizes

    # number of singular values to keep in each sector
    sub_max_bonds = [int(frac * s) for s in sizes]

    # distribute any remaining singular values to the smallest sectors
    rem = max_bond - sum(sub_max_bonds)

    for i in argsort(sub_max_bonds)[:rem]:
        sub_max_bonds[i] += 1

    return tuple(sub_max_bonds)


def svd_truncated(
    x, cutoff=-1.0, cutoff_mode=4, max_bond=-1, absorb=0, renorm=0
):
    """Truncated svd or raw array ``x``.

    Parameters
    ----------
    cutoff : float
        Singular value cutoff threshold.
    cutoff_mode : {1, 2, 3, 4, 5, 6}
        How to perform the trim:

            - 1: ['abs'], trim values below ``cutoff``
            - 2: ['rel'], trim values below ``s[0] * cutoff``
            - 3: ['sum2'], trim s.t. ``sum(s_trim**2) < cutoff``.
            - 4: ['rsum2'], trim s.t. ``sum(s_trim**2) < sum(s**2) * cutoff``.
            - 5: ['sum1'], trim s.t. ``sum(s_trim**1) < cutoff``.
            - 6: ['rsum1'], trim s.t. ``sum(s_trim**1) < sum(s**1) * cutoff``.

    max_bond : int
        An explicit maximum bond dimension, use -1 for none.
    absorb : {-1, 0, 1, None}
        How to absorb the singular values. -1: left, 0: both, 1: right and
        None: don't absorb (return).
    renorm : {0, 1}
        Whether to renormalize the singular values (depends on `cutoff_mode`).
    """
    backend = x.backend

    # first perform untruncated svd
    U, s, VH = svd(x)

    if renorm:
        raise NotImplementedError("renorm not implemented yet.")

    if cutoff > 0.0:
        # first combine all singular values into a single, sorted array
        sall = ar.do("concatenate", tuple(s.values()), like=backend)
        sall = ar.do("sort", sall, like=backend)

        if cutoff_mode == 1:
            # absolute cutoff
            abs_cutoff = cutoff
        elif cutoff_mode == 2:
            # relative cutoff
            abs_cutoff = sall[-1] * cutoff
        else:
            # possible square singular values
            power = {3: 2, 4: 2, 5: 1, 6: 1}[cutoff_mode]
            if power == 1:
                # sum1 or rsum1
                cum_spow = ar.do("cumsum", sall, 0, like=backend)
            else:
                # sum2 or rsum2
                cum_spow = ar.do("cumsum", sall**power, 0, like=backend)

            if cutoff_mode in (4, 6):
                # rsum1 or rsum2: relative cumulative cutoff
                cond = cum_spow > cutoff * cum_spow[-1]
            else:
                # sum1 or sum2: absolute cumulative cutoff
                cond = cum_spow > cutoff

            # translate to total number of singular values to keep
            n_chi_all = ar.do("count_nonzero", cond, like=backend)
            # and then to an absolute cutoff value
            abs_cutoff = sall[-n_chi_all]

        if 0 < max_bond < ar.size(sall):
            # also take into account a total maximum bond
            max_bond_cutoff = sall[-max_bond - 1]
            if max_bond_cutoff > abs_cutoff:
                abs_cutoff = max_bond_cutoff

        # now find number of values to keep per sector
        sub_max_bonds = [
            int(ar.do("count_nonzero", ss >= abs_cutoff, like=backend))
            for ss in s.values()
        ]
    else:
        # size of each sector
        sector_sizes = tuple(map(ar.size, s.values()))
        # distribute max_bond proportionally to sector sizes
        sub_max_bonds = calc_sub_max_bonds(sector_sizes, max_bond)

    for sector, n_chi in zip(s, sub_max_bonds):
        # check how many singular values from this sector are valid

        if n_chi == 0:
            # TODO: drop the block?
            raise NotImplementedError

        # slice the values and left and right vectors
        s[sector] = s[sector][:n_chi]
        U.blocks[sector] = U.blocks[sector][:, :n_chi]
        VH.blocks[sector] = VH.blocks[sector][:n_chi, :]

        # make sure the index chargemaps are updated too
        U.indices[-1].chargemap[sector[-1]] = n_chi
        VH.indices[0].chargemap[sector[0]] = n_chi

    if absorb is None:
        return U, s, VH

    # absorb the singular values block by block
    for sector in s:
        if absorb == -1:
            U.blocks[sector] *= s[sector].reshape((1, -1))
        elif absorb == 1:
            VH.blocks[sector] *= s[sector].reshape((-1, 1))
        elif absorb == 0:
            s_sqrt = ar.do("sqrt", s[sector], like=backend)
            U.blocks[sector] *= s_sqrt.reshape((1, -1))
            VH.blocks[sector] *= s_sqrt.reshape((-1, 1))

    return U, None, VH


# these are used by quimb for compressed contraction and gauging
ar.register_function("symmray", "qr_stabilized", qr_stabilized)
ar.register_function("symmray", "svd_truncated", svd_truncated)