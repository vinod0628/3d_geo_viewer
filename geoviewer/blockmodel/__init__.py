"""Block model management (no Qt): the protected binary block model file (.gvbm), block
models made from an ore solid or imported from a Surpac ASCII export, attribute editing,
and grade estimation (nearest neighbour, inverse distance, ordinary / simple kriging) with
variogram modelling and suggested search parameters.

Modules:

* ``store``      - the encrypted, compressed .gvbm file format
* ``build``      - new block model inside a solid, Surpac ASCII import
* ``attributes`` - add / rename / remove / fill attributes
* ``samples``    - estimation samples from drillholes (composited) or a Surpac point string file
* ``search``     - anisotropy and the neighbour search
* ``variogram``  - experimental variograms, models, automatic fitting, suggestions
* ``estimate``   - the estimators
"""

BLOCK_MODEL_EXT = ".gvbm"
