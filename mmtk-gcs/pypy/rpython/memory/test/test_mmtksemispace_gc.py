from rpython.memory.test import snippet
from rpython.memory.test.gc_test_base import GCTest

class TestMMTKSemiSpaceGC(GCTest, snippet.MMTKSemiSpaceGCTests):
    from rpython.memory.gc.mmtksemispace import MMTKSemiSpaceGC as GCClass
    GC_CAN_MOVE = True
