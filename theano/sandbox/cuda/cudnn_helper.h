#ifndef CUDNN_HELPER_H
#define CUDNN_HELPER_H

#include <cudnn.h>

// Define elements that are no longer available in the V5 interface in terms
// of elements of the previous versions.
#if defined(CUDNN_VERSION) && CUDNN_VERSION < 5000
#endif

#endif
