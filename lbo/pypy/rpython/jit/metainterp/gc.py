"""
Support for the various GCs.
"""

class GcDescription:
    def __init__(self, config):
        self.config = config


class GC_none(GcDescription):
    malloc_zero_filled = True

class GC_boehm(GcDescription):
    malloc_zero_filled = True

class GC_semispace(GcDescription):
    malloc_zero_filled = True

class GC_generation(GcDescription):
    malloc_zero_filled = True

class GC_hybrid(GcDescription):
    malloc_zero_filled = True

class GC_minimark(GcDescription):
    malloc_zero_filled = True

class GC_incminimark(GcDescription):
    malloc_zero_filled = False

# 1. mmtk should handle all objects <recommended>
# If not then it should be a root <not a better option>
class GC_mmtknogc(GcDescription):
    malloc_zero_filled = True

class GC_mmtksemispace(GcDescription):
    malloc_zero_filled = True


def get_description(config):
    name = config.translation.gc
    try:
        cls = globals()['GC_' + name]
    except KeyError:
        raise NotImplementedError('GC %r not supported by the JIT' % (name,))
    return cls(config)
