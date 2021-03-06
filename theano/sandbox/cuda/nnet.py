from theano import Op, Apply
from theano.compat.six import StringIO

from theano.sandbox.cuda import GpuOp
from theano.sandbox.cuda.basic_ops import as_cuda_ndarray_variable

from theano.sandbox.cuda.kernel_codegen import (nvcc_kernel,
                                                inline_softmax,
                                                inline_softmax_fixed_shared)


class GpuCrossentropySoftmaxArgmax1HotWithBias(GpuOp):
    """
    Implement CrossentropySoftmaxArgmax1HotWithBias on the gpu.
    """
    nin = 3
    nout = 3

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, x, b, y_idx):
        #N.B. won't work when we don't cast y_idx to float anymore
        nll = y_idx.type()
        sm = x.type()
        am = y_idx.type()
        return Apply(self, [x, b, y_idx], [nll, sm, am])

    def c_support_code(self):
        return """
        __global__ void k_xent_sm_1hot_bias(int M, int N,
            const float * x_data, int xs0, int xs1,
            const float * b, int bs0,
            const float * y_idx_data, int y_idxs0,
            float * nll_data, int nlls0,
            float * sm_data, int sms0, int sms1,
            float * am_data, int ams0)
        {
          for (int row = blockIdx.x; row < M; row += gridDim.x){

            const float * x = x_data + xs0 * row;
            const int y_idx = (int)y_idx_data[row * y_idxs0];
            float * sm = sm_data + sms0 * row;

            float sum = 0.0;
            int row_max_j = 0;
            float row_max = x[0] + b[0];
            for (int j = 1; j < N; ++j)
            {
                float row_ij = x[j*xs1] + b[j*bs0];
                //todo: store to shared memory
                row_max_j = (row_ij > row_max) ? j : row_max_j;
                row_max   = (row_ij > row_max) ? row_ij : row_max;
            }
            //compute the exp
            for (int j = 0; j < N; ++j)
            {
                float row_ij = x[j*xs1] + b[j*bs0];
                float sm_ij = exp(row_ij - row_max);
                sum += sm_ij;
                sm[j * sms1] = sm_ij;
            }
            float sum_inv = 1.0 / sum;
            for (int j = 0; j < N; ++j)
            {
                sm[j * sms1] *= sum_inv;
            }
            if ((y_idx >= N) || (y_idx < 0))
            {
                //TODO: set raise an error bit in a global var?
                nll_data[row*nlls0] = 0.0; // raise some suspicion at least...
            }
            else
            {
                nll_data[row*nlls0] = - x[y_idx*xs1]
                           - b[y_idx*bs0]
                           + row_max
                           + log(sum);
            }
            am_data[row*ams0] = row_max_j;
          }
        }

        """

    def c_code(self, node, nodename, inp, out, sub):
        x, b, y_idx = inp
        nll, sm, am = out
        classname = self.__class__.__name__
        fail = sub['fail']
        sio = StringIO()
        print >> sio, """
        if (CudaNdarray_NDIM(%(y_idx)s) != 1)
        {
            PyErr_SetString(PyExc_ValueError, "y_idx not 1d tensor");
            %(fail)s;
        }
        if (CudaNdarray_NDIM(%(x)s) != 2)
        {
            PyErr_SetString(PyExc_ValueError, "x not 2d tensor");
            %(fail)s;
        }
        if (CudaNdarray_NDIM(%(b)s) != 1)
        {
            PyErr_SetString(PyExc_ValueError, "b not 1d tensor");
            %(fail)s;
        }
        if (CudaNdarray_HOST_DIMS(%(x)s)[0] !=
            CudaNdarray_HOST_DIMS(%(y_idx)s)[0])
        {
            PyErr_SetString(PyExc_ValueError,
                            "dimension mismatch in x,y_idx arguments");
            %(fail)s;
        }
        if (CudaNdarray_HOST_DIMS(%(x)s)[1] != CudaNdarray_HOST_DIMS(%(b)s)[0])
        {
            PyErr_SetString(PyExc_ValueError,
                            "dimension mismatch in x,b arguments");
            %(fail)s;
        }
        if ((NULL == %(nll)s) //initial condition
            || (CudaNdarray_HOST_DIMS(%(nll)s)[0] !=
                CudaNdarray_HOST_DIMS(%(y_idx)s)[0]))
        {
            Py_XDECREF(%(nll)s);
            %(nll)s = (CudaNdarray*)CudaNdarray_NewDims(1,
                CudaNdarray_HOST_DIMS(%(y_idx)s));
            if(!%(nll)s)
            {
                %(fail)s;
            }
        }
        if ((NULL == %(sm)s)
            || (CudaNdarray_HOST_DIMS(%(sm)s)[0] !=
                CudaNdarray_HOST_DIMS(%(x)s)[0])
            || (CudaNdarray_HOST_DIMS(%(sm)s)[1] !=
                CudaNdarray_HOST_DIMS(%(x)s)[1]))
        {
            Py_XDECREF(%(sm)s);
            %(sm)s = (CudaNdarray*) CudaNdarray_NewDims(2,
                CudaNdarray_HOST_DIMS(%(x)s));
            if(!%(sm)s)
            {
                PyErr_SetString(PyExc_MemoryError,
                                "failed to alloc sm output");
                // no need to decref cnda_nll, the cleanup code should do it up
                %(fail)s;
            }
        }
        if ((NULL == %(am)s)
            || (CudaNdarray_HOST_DIMS(%(am)s)[0] !=
                CudaNdarray_HOST_DIMS(%(y_idx)s)[0]))
        {
            Py_XDECREF(%(am)s);
            %(am)s = (CudaNdarray*) CudaNdarray_NewDims(1,
                CudaNdarray_HOST_DIMS(%(y_idx)s));
            if(!%(am)s)
            {
                PyErr_SetString(PyExc_MemoryError,
                                "failed to alloc am output");
                // no need to decref nll and sm,
                // the cleanup code should do it up
                %(fail)s;
            }
        }
        {
            int n_blocks = std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                                    NUM_VECTOR_OP_BLOCKS);
     //TODO: launch more threads per row and do parallel sum and max reductions
            int n_threads = 1;
            int n_shared_bytes = 0; //n_threads * sizeof(float);

            k_xent_sm_1hot_bias<<<n_blocks, n_threads, n_shared_bytes>>>(
                CudaNdarray_HOST_DIMS(%(x)s)[0],
                CudaNdarray_HOST_DIMS(%(x)s)[1],
                CudaNdarray_DEV_DATA(%(x)s),
                CudaNdarray_HOST_STRIDES(%(x)s)[0],
                CudaNdarray_HOST_STRIDES(%(x)s)[1],
                CudaNdarray_DEV_DATA(%(b)s),
                CudaNdarray_HOST_STRIDES(%(b)s)[0],
                CudaNdarray_DEV_DATA(%(y_idx)s),
                CudaNdarray_HOST_STRIDES(%(y_idx)s)[0],
                CudaNdarray_DEV_DATA(%(nll)s),
                CudaNdarray_HOST_STRIDES(%(nll)s)[0],
                CudaNdarray_DEV_DATA(%(sm)s),
                CudaNdarray_HOST_STRIDES(%(sm)s)[0],
                CudaNdarray_HOST_STRIDES(%(sm)s)[1],
                CudaNdarray_DEV_DATA(%(am)s),
                CudaNdarray_HOST_STRIDES(%(am)s)[0]);
            CNDA_THREAD_SYNC;
            cudaError_t err = cudaGetLastError();
            if (cudaSuccess != err)
            {
                PyErr_Format(PyExc_RuntimeError,
                             "Cuda error: %(classname)s %(nodename)s: %%s.\\n"
                             "The kernel was launched with %%d threads,"
                             " %%d blocks and %%d shared memory\\n",
                             cudaGetErrorString(err),
                             n_threads, n_blocks, n_shared_bytes);
                // no need to decref output vars the cleanup code will do it
                %(fail)s;
            }
        }
        """ % locals()
        return sio.getvalue()

    def c_code_cache_version(self):
        #return ()
        return (4,)

gpu_crossentropy_softmax_argmax_1hot_with_bias = GpuCrossentropySoftmaxArgmax1HotWithBias()


class GpuCrossentropySoftmax1HotWithBiasDx(GpuOp):
    """
    Implement CrossentropySoftmax1HotWithBiasDx on the gpu.
    """
    nin = 3
    nout = 1
    """Gradient wrt x of the CrossentropySoftmax1Hot Op"""
    def __init__(self, **kwargs):
        Op.__init__(self, **kwargs)

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, dy, sm, y_idx):
        return Apply(self, [dy, sm, y_idx], [sm.type()])

    def c_code_cache_version(self):
        #return ()
        return (6,)

    def c_code(self, node, nodename, inp, out, sub):
        dnll, sm, y_idx = inp
        dx, = out
        fail = sub['fail']
        return """
        if ((CudaNdarray_NDIM(%(dnll)s) != 1)
            || (CudaNdarray_NDIM(%(sm)s) != 2)
            || (CudaNdarray_NDIM(%(y_idx)s) != 1))
        {
            PyErr_SetString(PyExc_ValueError, "rank error");
            %(fail)s;
        }
        if (CudaNdarray_HOST_DIMS(%(dnll)s)[0] !=
            CudaNdarray_HOST_DIMS(%(sm)s)[0])
        {
            PyErr_Format(PyExc_ValueError,
                         "dnll.shape[0] == %%i, but sm.shape[0] == %%i",
                         CudaNdarray_HOST_DIMS(%(dnll)s)[0],
                         CudaNdarray_HOST_DIMS(%(sm)s)[0]);
            %(fail)s;
        }
        if (CudaNdarray_HOST_DIMS(%(dnll)s)[0] !=
            CudaNdarray_HOST_DIMS(%(y_idx)s)[0])
        {
            PyErr_SetString(PyExc_ValueError,
                            "dnll.shape[0] != y_idx.shape[0]");
            %(fail)s;
        }
        if ((NULL == %(dx)s)
            || (CudaNdarray_HOST_DIMS(%(dx)s)[0] !=
                CudaNdarray_HOST_DIMS(%(sm)s)[0])
            || (CudaNdarray_HOST_DIMS(%(dx)s)[1] !=
                CudaNdarray_HOST_DIMS(%(sm)s)[1]))
        {
            Py_XDECREF(%(dx)s);
            %(dx)s = (CudaNdarray*)CudaNdarray_New();
            if ((NULL == %(dx)s)
                || CudaNdarray_alloc_contiguous(%(dx)s, 2,
                                                CudaNdarray_HOST_DIMS(%(sm)s)))
            {
                Py_XDECREF(%(dx)s);
                %(dx)s = NULL;
                %(fail)s;
            }
        }
        {
            int n_blocks = std::min(CudaNdarray_HOST_DIMS(%(dx)s)[0],
                                    NUM_VECTOR_OP_BLOCKS);
            int n_threads = std::min(CudaNdarray_HOST_DIMS(%(dx)s)[1],256);

            kCrossEntropySoftmax1HotWithBiasDx_%(nodename)s
                <<<n_blocks, n_threads>>>(
                        CudaNdarray_HOST_DIMS(%(dx)s)[0],
                        CudaNdarray_HOST_DIMS(%(dx)s)[1],

                        CudaNdarray_DEV_DATA(%(dnll)s),
                        CudaNdarray_HOST_STRIDES(%(dnll)s)[0],

                        CudaNdarray_DEV_DATA(%(sm)s),
                        CudaNdarray_HOST_STRIDES(%(sm)s)[0],
                        CudaNdarray_HOST_STRIDES(%(sm)s)[1],

                        CudaNdarray_DEV_DATA(%(y_idx)s),
                        CudaNdarray_HOST_STRIDES(%(y_idx)s)[0],

                        CudaNdarray_DEV_DATA(%(dx)s),
                        CudaNdarray_HOST_STRIDES(%(dx)s)[0],
                        CudaNdarray_HOST_STRIDES(%(dx)s)[1]
                );
            CNDA_THREAD_SYNC;
            cudaError_t err = cudaGetLastError();
            if( cudaSuccess != err)
            {
                PyErr_Format(PyExc_RuntimeError,
                             "Cuda error: %%s: %%s.\\n"
                             "The kernel was launched with %%d threads and"
                             " %%d blocks\\n",
                             "kCrossEntropySoftmax1HotWithBiasDx_%(nodename)s",
                             cudaGetErrorString(err), n_threads, n_blocks);
                %(fail)s;
            }
        }
        assert(%(dx)s);
        """ % locals()

    def c_support_code_apply(self, node, nodename):
        return """
        __global__ void kCrossEntropySoftmax1HotWithBiasDx_%(nodename)s(
           int N, int K,
           const float * dnll, const int dnll_s0,
           const float * sm, const int sm_s0, const int sm_s1,
           const float * y_idx, const int y_idx_s0,
           float * dx, const int dx_s0, const int dx_s1)
        {
            for (int i = blockIdx.x; i < N; i += gridDim.x)
            {
                float dnll_i = dnll[i * dnll_s0];
                int y_i = (int)y_idx[i * y_idx_s0];

                for (int j = threadIdx.x; j < K; j += blockDim.x)
                {
                    if (y_i == j)
                    {
                        dx[i * dx_s0 + j * dx_s1] =
                            dnll_i * (sm[i * sm_s0 + j * sm_s1]-1.0);
                    }
                    else
                    {
                        dx[i * dx_s0 + j * dx_s1] =
                            dnll_i * sm[i * sm_s0 + j * sm_s1];
                    }
                    //dx[i * dx_s0 + j * dx_s1] =
                    //    dnll_i * sm[i * sm_s0 + j * sm_s1];
                    //dx[i*dx_s0+j*dx_s1] = 0;
                }
            }
        }
        """ % locals()

gpu_crossentropy_softmax_1hot_with_bias_dx = GpuCrossentropySoftmax1HotWithBiasDx()


class GpuSoftmax(GpuOp):
    """
    Implement Softmax on the gpu.
    """
    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, x):
        return Apply(self, [x], [x.type()])

    def infer_shape(self, node, shape):
        return shape

    def c_code_cache_version(self):
        return (9,) + inline_softmax.code_version

    def c_code(self, node, nodename, inp, out, sub):
        x, = inp
        z, = out
        fail = sub['fail']
        return """
        if (CudaNdarray_NDIM(%(x)s) != 2)
        {
            PyErr_SetString(PyExc_ValueError, "rank error");
            %(fail)s;
        }
        if ((NULL == %(z)s) ||
            (CudaNdarray_HOST_DIMS(%(z)s)[0] !=
             CudaNdarray_HOST_DIMS(%(x)s)[0]) ||
            (CudaNdarray_HOST_DIMS(%(z)s)[1] !=
             CudaNdarray_HOST_DIMS(%(x)s)[1]))
        {
            Py_XDECREF(%(z)s);
            %(z)s = (CudaNdarray*)CudaNdarray_New();
            if ((NULL == %(z)s)
                || CudaNdarray_alloc_contiguous(%(z)s, 2,
                                                CudaNdarray_HOST_DIMS(%(x)s)))
            {
                Py_XDECREF(%(z)s);
                %(z)s = NULL;
                %(fail)s;
            }
        }
        {
            int n_blocks = std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],
                                    32 * 1024);
//TODO, detect the maximum number of thread per block.
            int n_threads = std::min(CudaNdarray_HOST_DIMS(%(x)s)[1], 512);
            int n_shared_bytes = CudaNdarray_HOST_DIMS(%(x)s)[1] *
                                     2 * sizeof(float);

            if (CudaNdarray_HOST_DIMS(%(x)s)[0] > 0)
            {
              //Those numbers are based on not too recent GPU
              //to make them compatible with more GPU.
              //TODO: read the information from the card.
              if(n_shared_bytes < (32 * 1024 - 500)){
                kSoftmax_%(nodename)s
                    <<<
                        n_blocks,
                        n_threads,
                        n_shared_bytes
                    >>>(
                            CudaNdarray_HOST_DIMS(%(x)s)[0],
                            CudaNdarray_HOST_DIMS(%(x)s)[1],

                            CudaNdarray_DEV_DATA(%(x)s),
                            CudaNdarray_HOST_STRIDES(%(x)s)[0],
                            CudaNdarray_HOST_STRIDES(%(x)s)[1],

                            CudaNdarray_DEV_DATA(%(z)s),
                            CudaNdarray_HOST_STRIDES(%(z)s)[0],
                            CudaNdarray_HOST_STRIDES(%(z)s)[1]
                    );
              }else{
                kSoftmax_fixed_shared%(nodename)s
                    <<<
                        n_blocks,
                        n_threads,
                        n_threads * sizeof(float)
                    >>>(
                            CudaNdarray_HOST_DIMS(%(x)s)[0],
                            CudaNdarray_HOST_DIMS(%(x)s)[1],

                            CudaNdarray_DEV_DATA(%(x)s),
                            CudaNdarray_HOST_STRIDES(%(x)s)[0],
                            CudaNdarray_HOST_STRIDES(%(x)s)[1],

                            CudaNdarray_DEV_DATA(%(z)s),
                            CudaNdarray_HOST_STRIDES(%(z)s)[0],
                            CudaNdarray_HOST_STRIDES(%(z)s)[1]
                    );
              }
              CNDA_THREAD_SYNC;
              cudaError_t err = cudaGetLastError();
              if( cudaSuccess != err)
              {
                  PyErr_Format(PyExc_RuntimeError,
                               "Cuda error: %%s: %%s.\\n Used %%d blocks,"
                               " %%d threads %%d bytes of shared memory",
                               "kSoftmax[_fixed_shared]%(nodename)s",
                               cudaGetErrorString(err),
                               n_blocks, n_threads, n_shared_bytes);
                  %(fail)s;
              }
            }
        }
        assert(%(z)s);
        """ % locals()

    def c_support_code_apply(self, node, nodename):
        ret1 = nvcc_kernel("kSoftmax_%s" % nodename,
                params=['int M', 'int N',
                        'const float * x', 'const int sx0', 'const int sx1',
                        'float * sm', 'const int sm_s0', 'const int sm_s1'],
                body=[
                    "extern __shared__ float buf[]",
                    "float * buf2 = buf + N",
                    "for (int blockIDX = blockIdx.x; blockIDX < M;"
                    "     blockIDX += gridDim.x){",
                      "for (int tx = threadIdx.x; tx< N; tx += blockDim.x){",
                        "buf[tx] = x[blockIDX * sx0 + tx * sx1]",
                        "buf2[tx] = buf[tx]",
                      "}",
                      "__syncthreads()",
                      inline_softmax('N', 'buf', 'buf2',
                                     'threadIdx.x', 'blockDim.x'),
                      "for (int tx = threadIdx.x; tx< N; tx += blockDim.x){",
                        # This set all value correctly
                        "sm[blockIDX * sm_s0 + tx * sm_s1] = buf[tx]",
                      "}",
                      "__syncthreads()",
                    "}",
                ])
        ret2 = nvcc_kernel("kSoftmax_fixed_shared%s" % nodename,
                params=['int M', 'int N',
                        'const float * x', 'const int sx0', 'const int sx1',
                        'float * sm', 'const int sm_s0', 'const int sm_s1'],
                body=[
                    "extern __shared__ float buf[]",
                    "for (int blockIDX = blockIdx.x; blockIDX < M;"
                    "     blockIDX += gridDim.x){",
                      "const float *x_ptr = &x[blockIDX * sx0]",
                      "float *sm_ptr = &sm[blockIDX * sm_s0]",
                      inline_softmax_fixed_shared('N', 'buf', 'x_ptr', 'sx1',
                                                  'sm_ptr', 'sm_s1',
                                                  'threadIdx.x', 'blockDim.x'),
                      "__syncthreads()",
                    "}",
                    ])
        return ret1 + "\n" + ret2

gpu_softmax = GpuSoftmax()


class GpuSoftmaxWithBias(GpuOp):
    """
    Implement SoftmaxWithBias on the gpu.
    """
    nin = 2
    nout = 1

    def __eq__(self, other):
        return type(self) == type(other)

    def __hash__(self):
        return hash(type(self))

    def __str__(self):
        return self.__class__.__name__

    def make_node(self, x, b):
        return Apply(self, [x, b], [x.type()])

    def infer_shape(self, node, shape):
        return [shape[0]]

    def c_code_cache_version(self):
        #return ()
        return (9,) + inline_softmax.code_version

    def c_code(self, node, nodename, inp, out, sub):
        x, b = inp
        z, = out
        fail = sub['fail']
        return """
        if (CudaNdarray_NDIM(%(x)s) != 2)
        {
            PyErr_SetString(PyExc_ValueError, "rank error input");
            %(fail)s;
        }
        if (CudaNdarray_NDIM(%(b)s) != 1)
        {
            PyErr_SetString(PyExc_ValueError, "rank error for the bias");
            %(fail)s;
        }
        if ((CudaNdarray_HOST_DIMS(%(x)s)[1] !=
            CudaNdarray_HOST_DIMS(%(b)s)[0]))
        {
            PyErr_Format(PyExc_ValueError,
                         "number of columns in x (%%ld)"
                         " does not match length of b (%%ld)",
                         (long int)CudaNdarray_HOST_DIMS(%(x)s)[1],
                         (long int)CudaNdarray_HOST_DIMS(%(b)s)[0]);
            %(fail)s;
        }
        if ((NULL == %(z)s)
            || (CudaNdarray_HOST_DIMS(%(z)s)[0] !=
                CudaNdarray_HOST_DIMS(%(x)s)[0])
            || (CudaNdarray_HOST_DIMS(%(z)s)[1] !=
                CudaNdarray_HOST_DIMS(%(x)s)[1]))
        {
            Py_XDECREF(%(z)s);
            %(z)s = (CudaNdarray*)CudaNdarray_New();
            if ((NULL == %(z)s)
                || CudaNdarray_alloc_contiguous(%(z)s, 2,
                       CudaNdarray_HOST_DIMS(%(x)s)))
            {
                Py_XDECREF(%(z)s);
                %(z)s = NULL;
                %(fail)s;
            }
        }
        {
            int n_blocks = std::min(CudaNdarray_HOST_DIMS(%(x)s)[0],32*1024);
//TODO, detect the maximum number of thread per block.
            int n_threads = std::min(CudaNdarray_HOST_DIMS(%(x)s)[1], 512);
            int n_shared_bytes = CudaNdarray_HOST_DIMS(%(x)s)[1] *
                                     2 * sizeof(float);
            if (CudaNdarray_HOST_DIMS(%(x)s)[0] > 0)
            {
              if(n_shared_bytes < (32 * 1024 - 500)){
                kSoftmaxWithBias_%(nodename)s
                    <<<
                        n_blocks,
                        n_threads,
                        n_shared_bytes
                    >>>(
                        CudaNdarray_HOST_DIMS(%(x)s)[0],
                        CudaNdarray_HOST_DIMS(%(x)s)[1],

                        CudaNdarray_DEV_DATA(%(x)s),
                        CudaNdarray_HOST_STRIDES(%(x)s)[0],
                        CudaNdarray_HOST_STRIDES(%(x)s)[1],

                        CudaNdarray_DEV_DATA(%(b)s),
                        CudaNdarray_HOST_STRIDES(%(b)s)[0],

                        CudaNdarray_DEV_DATA(%(z)s),
                        CudaNdarray_HOST_STRIDES(%(z)s)[0],
                        CudaNdarray_HOST_STRIDES(%(z)s)[1]
                    );
              }else{
                kSoftmaxWithBias_fixed_shared%(nodename)s
                    <<<
                        n_blocks,
                        n_threads,
                        n_threads * sizeof(float)
                    >>>(
                        CudaNdarray_HOST_DIMS(%(x)s)[0],
                        CudaNdarray_HOST_DIMS(%(x)s)[1],

                        CudaNdarray_DEV_DATA(%(x)s),
                        CudaNdarray_HOST_STRIDES(%(x)s)[0],
                        CudaNdarray_HOST_STRIDES(%(x)s)[1],

                        CudaNdarray_DEV_DATA(%(b)s),
                        CudaNdarray_HOST_STRIDES(%(b)s)[0],

                        CudaNdarray_DEV_DATA(%(z)s),
                        CudaNdarray_HOST_STRIDES(%(z)s)[0],
                        CudaNdarray_HOST_STRIDES(%(z)s)[1]
                    );
              }
                CNDA_THREAD_SYNC;
                cudaError_t err = cudaGetLastError();
                if( cudaSuccess != err)
                {
                    PyErr_Format(PyExc_RuntimeError,
                                 "Cuda error: %%s: %%s. n_blocks=%%d,"
                                 " n_threads=%%d, n_shared_bytes=%%d\\n",
                                 "kSoftmaxWithBias_%(nodename)s",
                                 cudaGetErrorString(err),
                                 n_blocks, n_threads, n_shared_bytes);
                    %(fail)s;
                }
            }
        }
        assert(%(z)s);
        """ % locals()

    def c_support_code_apply(self, node, nodename):
        ret1 = nvcc_kernel(
            "kSoftmaxWithBias_%s" % nodename,
            params=['int M', 'int N',
                    'const float * x', 'const int sx0', 'const int sx1',
                    'const float * b', 'const int sb0',
                    'float * sm', 'const int sm_s0', 'const int sm_s1'],
            body=[
                    "extern __shared__ float buf[]",
                    "float * buf2 = buf + N",
                    "for (int blockIDX = blockIdx.x; blockIDX < M;"
                    "     blockIDX += gridDim.x){",
                      "for (int tx = threadIdx.x; tx< N; tx += blockDim.x){",
                         "buf[tx] = x[blockIDX * sx0 + tx * sx1]",
                         "buf[tx] += b[tx * sb0]",
                         "buf2[tx] = buf[tx]",
                      "}",
                       "__syncthreads()",
                       inline_softmax('N', 'buf', 'buf2',
                                      'threadIdx.x', 'blockDim.x'),
                      "for (int tx = threadIdx.x; tx< N; tx += blockDim.x){",
                         "sm[blockIDX * sm_s0 + tx * sm_s1] = buf[tx]",
                      "}",
                      "__syncthreads()",
                    "}",
            ])
        ret2 = nvcc_kernel("kSoftmaxWithBias_fixed_shared%s" % nodename,
                           params=['int M', 'int N',
                                   'const float * x',
                                   'const int sx0', 'const int sx1',
                                   'const float * b', 'const int sb0',
                                   'float * sm',
                                   'const int sm_s0', 'const int sm_s1'],
                           body=[
                               "extern __shared__ float buf[]",
                               "for (int blockIDX = blockIdx.x; blockIDX < M;"
                               "     blockIDX += gridDim.x){",
                               "const float *x_ptr = &x[blockIDX * sx0]",
                               "float *sm_ptr = &sm[blockIDX * sm_s0]",
                               inline_softmax_fixed_shared('N', 'buf',
                                                           'x_ptr', 'sx1',
                                                           'sm_ptr', 'sm_s1',
                                                           'threadIdx.x',
                                                           'blockDim.x',
                                                           'b', 'sb0'),
                               "__syncthreads()",
                               "}",
                           ])
        return ret1 + "\n" + ret2

gpu_softmax_with_bias = GpuSoftmaxWithBias()
