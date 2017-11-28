# Copyright (c) 2014 Evalf
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
The transform module.
"""

from . import cache, numeric, _
import numpy, collections, itertools, functools, operator


## TRANSFORM CHAIN OPERATIONS

def apply(chain, points):
  for trans in reversed(chain):
    points = trans.apply(points)
  return points

def transform_poly(trans, coeffs):
  for item in trans:
    coeffs = item.transform_poly(coeffs)
  return coeffs

def n_ascending(chain):
  # number of ascending transform items counting from root (0). this is a
  # temporary hack required to deal with Bifurcate/Slice; as soon as we have
  # proper tensorial topologies we can switch back to strictly ascending
  # transformation chains.
  for n, trans in enumerate(chain):
    if trans.todims is not None and trans.todims < trans.fromdims:
      return n
  return len(chain)

def canonical(chain):
  # keep at lowest ndims possible; this is the required form for bisection
  n = n_ascending(chain)
  if n < 2:
    return tuple(chain)
  items = list(chain)
  i = 0
  while items[i].fromdims > items[n-1].fromdims:
    swapped = items[i+1].swapdown(items[i])
    if swapped:
      items[i:i+2] = swapped
      i -= i > 0
    else:
      i += 1
  return tuple(items)

def promote(chain, ndims):
  # split chain into chain1 and chain2 such that chain == chain1 << chain2 and
  # chain1.fromdims == chain2.todims == ndims, where chain1 is canonical and
  # chain2 climbs to ndims as fast as possible.
  n = n_ascending(chain)
  assert ndims >= chain[n-1].fromdims
  items = list(chain)
  i = n
  while items[i-1].fromdims < ndims:
    swapped = items[i-2].swapup(items[i-1])
    if swapped:
      items[i-2:i] = swapped
      i += i < n
    else:
      i -= 1
  assert items[i-1].fromdims == ndims
  return canonical(items[:i]), tuple(items[i:])

def lookup(chain, transforms):
  if not transforms:
    return
  for trans in transforms:
    ndims = trans[-1].fromdims
    break
  head, tail = promote(chain, ndims)
  while head:
    if head in transforms:
      return head, tail
    tail = head[-1:] + tail
    head = head[:-1]

def lookup_item(chain, transforms):
  head_tail = lookup(chain, transforms)
  if not head_tail:
    raise KeyError(chain)
  head, tail = head_tail
  item = transforms[head] if isinstance(transforms, collections.Mapping) else transforms.index(head)
  return item, tail

def linearfrom(chain, ndims):
  if chain and ndims < chain[-1].fromdims:
    for i in reversed(range(len(chain))):
      if chain[i].todims == ndims:
        chain = chain[:i]
        break
    else:
      raise Exception( 'failed to find {}D coordinate system'.format(ndims) )
  if not chain:
    return numpy.eye(ndims)
  scale = 1.
  linear = numpy.eye(chain[-1].fromdims)
  for trans in reversed(chain):
    if isinstance(trans, Scale):
      scale *= trans.scale
    else:
      linear = numpy.dot(trans.linear, linear)
      if isinstance(trans, Updim):
        linear = numpy.concatenate([linear, trans.ext[:,_]], axis=1)
  linear *= scale
  n, m = linear.shape
  if m >= ndims:
    return linear[:,:ndims]
  return numpy.concatenate([linear, numpy.zeros((n,ndims-m))], axis=1)


## TRANSFORM ITEMS

class TransformItem(cache.Immutable):

  def __init__(self, todims, fromdims:int):
    self.todims = todims
    self.fromdims = fromdims

  def __repr__(self):
    return '{}({})'.format(self.__class__.__name__, self)

  def swapup(self, other):
    return None

  def swapdown(self, other):
    return None

class Bifurcate(TransformItem):

  def __init__(self, trans1:canonical, trans2:canonical):
    fromdims = trans1[-1].fromdims + trans2[-1].fromdims
    self.trans1 = trans1 + (Slice(0, trans1[-1].fromdims, fromdims),)
    self.trans2 = trans2 + (Slice(trans1[-1].fromdims, fromdims, fromdims),)
    super().__init__(todims=trans1[0].todims if trans1[0].todims == trans2[0].todims else None, fromdims=fromdims)

  def apply(self, points):
    return apply(self.trans1, points), apply(self.trans2, points)

class Matrix(TransformItem):

  def __init__(self, linear:numeric.const, offset:numeric.const):
    assert linear.ndim == 2 and linear.dtype == float
    assert offset.ndim == 1 and offset.dtype == float
    assert len(offset) == len(linear)
    self.linear = linear
    self.offset = offset
    super().__init__(linear.shape[0], linear.shape[1])

  def apply(self, points):
    assert points.shape[-1] == self.fromdims
    return numeric.const(numpy.dot( points, self.linear.T ) + self.offset, copy=False)

  def __mul__(self, other):
    assert isinstance(other, Matrix) and self.fromdims == other.todims
    linear = numpy.dot(self.linear, other.linear)
    offset = self.apply(other.offset)
    return Square(linear, offset) if self.todims == other.fromdims \
      else Updim(linear, offset, self.isflipped^other.isflipped) if self.todims == other.fromdims+1 \
      else Matrix(linear, offset)

  def __str__( self ):
    return numeric.fstr(self.offset) + ''.join('+{}*x{}'.format(numeric.fstr(v), i) for i, v in enumerate(self.linear.T))

class Square(Matrix):

  def __init__(self, linear:numeric.const, offset:numeric.const):
    self._transform_matrix = {}
    super().__init__(linear, offset)

  def invapply(self, points):
    return numeric.const(numpy.linalg.solve(self.linear, points - self.offset), copy=False)

  @cache.property
  def det(self):
    return numeric.det_exact(self.linear)

  @property
  def isflipped(self):
    return self.det < 0

  def transform_poly(self, coeffs):
    assert coeffs.ndim == self.fromdims + 1
    degree = coeffs.shape[1] - 1
    assert all(n == degree+1 for n in coeffs.shape[2:])
    try:
      M = self._transform_matrix[degree]
    except KeyError:
      eye = numpy.eye(self.fromdims, dtype=int)
      # construct polynomials for affine transforms of individual dimensions
      polys = numpy.zeros((self.fromdims,)+(2,)*self.fromdims)
      polys[(slice(None),)+(0,)*self.fromdims] = self.offset
      for idim, e in enumerate(eye):
        polys[(slice(None),)+tuple(e)] = self.linear[:,idim]
      # reduces polynomials to smallest nonzero power
      polys = [poly[tuple(slice(None if p else 1) for p in poly[tuple(eye)])] for poly in polys]
      # construct transform poly by transforming all monomials separately and summing
      M = numpy.zeros((degree+1,)*(2*self.fromdims), dtype=float)
      for powers in numpy.ndindex(*[degree+1]*self.fromdims):
        if sum(powers) <= degree:
          M_power = functools.reduce(numeric.poly_mul, [numeric.poly_pow(poly, power) for poly, power in zip(polys, powers)])
          M[tuple(slice(n) for n in M_power.shape)+powers] += M_power
      self._transform_matrix[degree] = M
    return numpy.einsum('jk,ik', M.reshape([(degree+1)**self.fromdims]*2), coeffs.reshape(coeffs.shape[0],-1)).reshape(coeffs.shape)

class Shift(Square):

  det = 1.

  def __init__(self, offset:numeric.const):
    assert offset.ndim == 1 and offset.dtype == float
    super().__init__(numpy.eye(len(offset)), offset)

  def apply(self, points):
    return numeric.const(points + self.offset, copy=False)

  def invapply(self, points):
    return numeric.const(points - self.offset, copy=False)

  def __str__(self):
    return '{}+x'.format(numeric.fstr(self.offset))

class Identity(Shift):

  def __init__(self, ndims):
    super().__init__(numpy.zeros(ndims))

  def apply(self, points):
    return points

  def invapply(self, points):
    return points

  def __str__(self):
    return 'x'

class Scale(Square):

  def __init__(self, scale:float, offset:numeric.const):
    assert offset.ndim == 1 and offset.dtype == float
    self.scale = scale
    super().__init__(numpy.eye(len(offset)) * scale, offset)

  def apply(self, points):
    return numeric.const(self.scale * points + self.offset, copy=False)

  def invapply(self, points):
    return numeric.const((points - self.offset) / self.scale, copy=False)

  @property
  def det(self):
    return self.scale**self.todims

  def __str__(self):
    return '{}+{}*x'.format( numeric.fstr(self.offset), numeric.fstr(self.linear) )

  def __mul__(self, other):
    assert isinstance(other, Matrix) and self.fromdims == other.todims
    if isinstance(other, Scale):
      return Scale(self.scale * other.scale, self.apply(other.offset))
    return super().__mul__(other)

class Updim(Matrix):

  def __init__(self, linear:numeric.const, offset:numeric.const, isflipped:bool):
    assert linear.shape[0] > linear.shape[1]
    self.isflipped = isflipped
    super().__init__(linear, offset)

  @cache.property
  def ext(self):
    ext = numeric.ext( self.linear )
    return -ext if self.isflipped else ext

  @property
  def flipped(self):
    return Updim(self.linear, self.offset, not self.isflipped)

  @cache.property
  def orthoaxes(self):
    # returns a tuple of indices such that eye(todims).take(orthoaxes,
    # axis=0).dot(self.linear) == eye(fromdims), if such a set exists; this
    # coincides with all the simplex and tensor element edges for which edge
    # and child transforms can be swapped. used in swapdown.
    orthoaxes = []
    for e in numpy.eye(self.fromdims):
      i, = numpy.equal(self.linear, e).all(axis=1).nonzero()
      if len(i) != 1:
        return
      orthoaxes.append(i[0])
    return tuple(orthoaxes)

  def swapup(self, other):
    # prioritize ascending transformations, i.e. change updim << scale to scale << updim
    if self.orthoaxes is not None and isinstance(other, Scale) and other.scale == .5:
      return Scale(.5, self.apply(other.offset) - .5 * self.offset), self

  def swapdown(self, other):
    # prioritize decending transformations, i.e. change scale << updim to updim << scale
    if isinstance(other, Scale) and other.scale == .5:
      orthoaxes = self.orthoaxes
      if orthoaxes is not None:
        newlinear = .5 * self.linear.take(orthoaxes, axis=0)
        newoffset = (other.apply(self.offset) - self.offset).take(orthoaxes, axis=0)
        if numpy.equal(newlinear, numpy.eye(len(newlinear)) * .5).all():
          newtrans = Scale(.5, newoffset)
        else:
          newtrans = Matrix(newlinear, newoffset)
        if self * newtrans == other * self:
          return self, newtrans
      return ScaledUpdim(other, self), Identity(self.fromdims)

class Slice(Matrix):

  def __init__(self, i1:int, i2:int, fromdims:int):
    todims = i2-i1
    assert 0 <= todims <= fromdims
    self.s = slice(i1,i2)
    super().__init__(numpy.eye(fromdims)[self.s], numpy.zeros(todims))

  def apply(self, points):
    return numeric.const(points[:,self.s])

class ScaledUpdim(Updim):

  def __init__(self, trans1, trans2):
    assert trans1.todims == trans1.fromdims == trans2.todims == trans2.fromdims + 1
    self.trans1 = trans1
    self.trans2 = trans2
    super().__init__(numpy.dot(trans1.linear, trans2.linear), trans1.apply(trans2.offset), trans1.isflipped^trans2.isflipped)

  def swapup(self, other):
    if isinstance(other, Identity):
      return self.trans1, self.trans2

class VertexTransform(TransformItem):

  def __init__(self, fromdims:int):
    super().__init__(None, fromdims)

class MapTrans(VertexTransform):

  def __init__(self, linear:numeric.const, offset:numeric.const, vertices:numeric.const):
    assert len(linear) == len(offset) == len(vertices)
    self.vertices, self.linear, self.offset = map( numpy.array, zip( *sorted( zip( vertices, linear, offset ) ) ) ) # sort vertices
    super().__init__(self.linear.shape[1])

  def apply(self, points):
    barycentric = numpy.dot( points, self.linear.T ) + self.offset
    return tuple( tuple( (v,float(c)) for v, c in zip( self.vertices, coord ) if c ) for coord in barycentric )

  def __str__(self):
    return ','.join( str(v) for v in self.vertices )

class RootTrans(VertexTransform):

  def __init__(self, name, shape:tuple):
    self.I, = numpy.where( shape )
    self.w = numpy.take( shape, self.I )
    self.name = name
    super().__init__(len(shape))

  def apply(self, coords):
    coords = numpy.asarray(coords)
    assert coords.ndim == 2
    if self.I.size:
      coords = coords.copy()
      coords[:,self.I] %= self.w
    return tuple(self.name + str(c) for c in coords.tolist())

  def __str__(self):
    return repr(self.name + '[*]')

class RootTransEdges(VertexTransform):

  def __init__(self, name, shape:tuple):
    self.shape = shape
    assert numeric.isarray(name)
    assert name.shape == (3,)*len(shape)
    self.name = name.copy()
    super().__init__(len(shape))

  def apply(self, coords):
    assert coords.ndim == 2
    labels = []
    for coord in coords.T.frac.T:
      right = (coord[:,1]==1) & (coord[:,0]==self.shape)
      left = coord[:,0]==0
      where = (1+right)-left
      s = self.name[tuple(where)] + '[%s]' % ','.join(str(n) if d == 1 else '%d/%d' % (n,d) for n, d in coord[where==1])
      labels.append(s)
    return labels

  def __str__(self):
    return repr(','.join(self.name.flat)+'*')


## CONSTRUCTORS

def simplex(coords, isflipped):
  coords = numpy.asarray(coords)
  offset = coords[0]
  return Updim((coords[1:]-offset).T, offset, isflipped=isflipped)

def tensor(trans1, trans2):
  offset = numpy.concatenate([trans1.offset, trans2.offset])
  return Scale(trans1.scale, offset) if isinstance(trans1, Scale) and isinstance(trans2, Scale) and trans1.scale == trans2.scale \
    else Square(numeric.blockdiag([trans1.linear, trans2.linear]), offset)

# vim:shiftwidth=2:softtabstop=2:expandtab:foldmethod=indent:foldnestmax=2
