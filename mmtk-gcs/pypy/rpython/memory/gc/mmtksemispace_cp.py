from rpython.rtyper.lltypesystem.llmemory import raw_malloc, raw_free
from rpython.rtyper.lltypesystem.llmemory import raw_memcopy, raw_memclear
from rpython.rtyper.lltypesystem.llmemory import NULL, raw_malloc_usage
from rpython.memory.support import get_address_stack, get_address_deque
from rpython.memory.support import AddressDict
from rpython.rtyper.lltypesystem import lltype, llmemory, llarena, rffi, llgroup
from rpython.rlib.objectmodel import free_non_gc_object
from rpython.rlib.debug import ll_assert, have_debug_prints
from rpython.rlib.debug import debug_print, debug_start, debug_stop
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rlib.rarithmetic import ovfcheck, LONG_BIT
from rpython.memory.gc.base import MovingGCBase, ARRAY_TYPEID_MAP,\
     TYPEID_MAP
from rpython.rtyper.tool.rffi_platform import llexternal_mmtk

import sys, os

GCFLAG_HASHMASK = _GCFLAG_HASH_BASE * 0x3
GC_HASH_NOTTAKEN   = _GCFLAG_HASH_BASE * 0x0
GC_HASH_TAKEN_ADDR = _GCFLAG_HASH_BASE * 0x1
GC_HASH_TAKEN_NURS = _GCFLAG_HASH_BASE * 0x2
GC_HASH_HASFIELD   = _GCFLAG_HASH_BASE * 0x3

GCFLAG_EXTRA = first_gcflag << 5

memoryError = MemoryError()

#init
ll_mmtk_gc_init = llexternal_mmtk('pypy_gc_init', [], lltype.Void)
ll_mmtk_is_gc_initialized = llexternal_mmtk('pypy_is_gc_initialized', [], lltype.Bool)
ll_mmtk_mmtk_set_heap_size = llexternal_mmtk('mmtk_set_heap_size', [rffi.SIGNED, rffi.SIGNED], lltype.Bool)

#mutator
ll_mmtk_bind_mutator = llexternal_mmtk('bind_mutator', [], rffi.VOIDP)
ll_mmtk_destroy_mutator = llexternal_mmtk('destroy_mutator', [rffi.VOIDP], lltype.Void)
ll_mmtk_flush_mutator = llexternal_mmtk('flush_mutator', [rffi.VOIDP], lltype.Void)

# finalizer
ll_mmtk_add_finalizer  = llexternal_mmtk('add_finalizer', [rffi.VOIDP], lltype.Void)
ll_mmtk_get_finalized_object  = llexternal_mmtk('get_finalized_object', [], rffi.VOIDP)

#weakreferences
ll_mmtk_add_weak_candidate  = llexternal_mmtk('add_weak_candidate', [rffi.VOIDP], lltype.Void)

#allocation
ll_mmtk_alloc = llexternal_mmtk('mmtk_alloc', [rffi.VOIDP, rffi.SIGNED, rffi.SIGNED, rffi.SIGNED], lltype.Unsigned)
ll_mmtk_post_alloc = llexternal_mmtk('post_alloc', [rffi.VOIDP, rffi.INT, rffi.INT], lltype.Void)

#scan
ll_mmtk_is_live_object = llexternal_mmtk('mmtk_is_live_object', [rffi.VOIDP], lltype.Bool)
ll_mmtk_is_mmtk_object = llexternal_mmtk('mmtk_is_mmtk_object ', [rffi.VOIDP], lltype.Bool)

# collect etc
ll_mmtk_will_never_move = llexternal_mmtk('mmtk_will_never_move ', [rffi.VOIDP], lltype.Bool)
ll_mmtk_handle_user_collection_request = llexternal_mmtk('mmtk_handle_user_collection_request', [rffi.VOIDP], lltype.Void)
ll_mmtk_plan_name = llexternal_mmtk('mmtk_plan_name ', [rffi.VOID], lltype.VoidP)

#stats
ll_mmtk_free_bytes  = llexternal_mmtk('mmtk_free_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_total_bytes = llexternal_mmtk('mmtk_total_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_used_bytes = llexternal_mmtk('mmtk_used_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_starting_heap_address = llexternal_mmtk('mmtk_starting_heap_address', [rffi.VOID], lltype.VoidP)
ll_mmtk_last_heap_address = llexternal_mmtk('mmtk_last_heap_address ', [rffi.VOID], lltype.VoidP)

class MMTKSemiSpaceGC(MovingGCBase):
    _alloc_flavor_ = "raw"
    inline_simple_malloc = True
    inline_simple_malloc_varsize = True
    malloc_zero_filled = True
    first_unused_gcflag = first_gcflag << 6
    gcflag_extra = GCFLAG_EXTRA

    HDR = lltype.Struct('header', ('tid', lltype.Signed))
    typeid_is_in_field = 'tid'
    FORWARDSTUB = lltype.GcStruct('forwarding_stub',
                                  ('forw', llmemory.Address))
    FORWARDSTUBPTR = lltype.Ptr(FORWARDSTUB)

    object_minimal_size = llmemory.sizeof(FORWARDSTUB)

    TRANSLATION_PARAMS = {'space_size': 8*1024*1024}

    def __init__(self, config, space_size=4096, max_space_size=sys.maxint//2+1,
                 **kwds):
        self.param_space_size = space_size
        self.param_max_space_size = max_space_size
        MovingGCBase.__init__(self, config, **kwds)
        ll_mmtk_gc_init() 
        ll_assert(ll_mmtk_is_gc_initialized() == True, 
        "MMTK initialized successfully")
        ll_assert(ll_mmtk_mmtk_set_heap_size(self.param_space_size, self.param_max_space_size)  == True,
        "MMTK heap size set successfully")
        self.handle = ll_mmtk_bind_mutator()

    def setup(self):
        self.space_size = self.param_space_size
        self.max_space_size = self.param_max_space_size
        self.mmtk_handle = self.handle
        MovingGCBase.setup(self)
        self.objects_with_finalizers = self.AddressDeque()
        self.objects_with_light_finalizers = self.AddressStack()
        self.objects_with_weakrefs = self.AddressStack()

    def _teardown(self):
        debug_print("Teardown")
        ll_mmtk_destroy_mutator(self.mmtk_handle)
        ll_mmtk_flush_mutator(self.mmtk_handle)

    def malloc_fixedsize_clear(self, typeid16, size,
                               has_finalizer=False,
                               is_finalizer_light=False,
                               contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + size
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        llarena.arena_reserve(result, totalsize)
        self.init_gc_object(result, typeid16)
        self.free = result + totalsize
        if has_finalizer:
            from rpython.rtyper.lltypesystem import rffi
            self.objects_with_finalizers.append(result + size_gc_header)
            self.objects_with_finalizers.append(rffi.cast(llmemory.Address, -1))
            ll_mmtk_add_finalizer(llmemory.cast_adr_to_ptr(result+size_gc_header, llmemory.GCREF))
        if contains_weakptr:
            self.objects_with_weakrefs.append(result + size_gc_header)
            ll_mmtk_add_weak_candidate(llmemory.cast_adr_to_ptr(result+size_gc_header, llmemory.GCREF))
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result+size_gc_header, llmemory.GCREF)

    def malloc_varsize_clear(self, typeid16, length, size, itemsize,
                             offset_to_length):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        nonvarsize = size_gc_header + size
        try:
            varsize = ovfcheck(itemsize * length)
            totalsize = ovfcheck(nonvarsize + varsize)
        except OverflowError:
            raise memoryError
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        llarena.arena_reserve(result, totalsize)
        self.init_gc_object(result, typeid16)
        (result + size_gc_header + offset_to_length).signed[0] = length
        self.free = result + llarena.round_up_for_allocation(totalsize)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result+size_gc_header, llmemory.GCREF)

    def shrink_array(self, addr, smallerlength):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        if self._is_in_the_space(addr - size_gc_header):
            typeid = self.get_type_id(addr)
            totalsmallersize = (
                size_gc_header + self.fixed_size(typeid) +
                self.varsize_item_sizes(typeid) * smallerlength)
            llarena.arena_shrink_obj(addr - size_gc_header, totalsmallersize)
            #
            offset_to_length = self.varsize_offset_to_length(typeid)
            (addr + offset_to_length).signed[0] = smallerlength
            return True
        else:
            return False

    def register_finalizer(self, fq_index, gcobj):
        from rpython.rtyper.lltypesystem import rffi
        ll_mmtk_add_finalizer(gcobj)

    def obtain_free_space(self, needed):
        return NotImplemented
    obtain_free_space._dont_inline_ = True

    def try_obtain_free_space(self, needed):
        return NotImplemented

    def double_space_size(self):
        return NotImplemented

    def set_max_heap_size(self, size):
        self.max_heap_size = float(size)
        ll_mmtk_mmtk_set_heap_size(0, self.max_heap_size)

    @classmethod
    def JIT_minimal_size_in_nursery(cls):
        return cls.object_minimal_size

    def collect(self, gen=0):
        self.debug_check_consistency()
        return NotImplemented

    def semispace_collect(self, size_changing=False):
         return NotImplemented

    def starting_full_collect(self):
        pass    # hook for the HybridGC

    def finished_full_collect(self):
        pass    # hook for the HybridGC

    def record_red_zone(self):
        return NotImplemented

    def get_size_incl_hash(self, obj):
        size = self.get_size(obj)
        hdr = self.header(obj)
        if (hdr.tid & GCFLAG_HASHMASK) == GC_HASH_HASFIELD:
            size += llmemory.sizeof(lltype.Signed)
        return size

    def scan_copied(self, scan):
        while scan < self.free:
            curr = scan + self.size_gc_header()
            self.trace_and_copy(curr)
            scan += self.size_gc_header() + self.get_size_incl_hash(curr)
        return scan

    def collect_roots(self):
        self.root_walker.walk_roots(
            MMTKSemiSpaceGC._collect_root,  # stack roots
            MMTKSemiSpaceGC._collect_root,  # static in prebuilt non-gc structures
            MMTKSemiSpaceGC._collect_root)  # static in prebuilt gc objects

    def _collect_root(self, root):
        root.address[0] = self.copy(root.address[0])

    def copy(self, obj):
        if self.DEBUG:
            self.debug_check_can_copy(obj)
        if self.is_forwarded(obj):
            return self.get_forwarding_address(obj)
        else:
            objsize = self.get_size(obj)
            newobj = self.make_a_copy(obj, objsize)
            self.set_forwarding_address(obj, newobj, objsize)
            return newobj

    def _get_object_hash(self, obj, objsize, tid):
        gc_hash = tid & GCFLAG_HASHMASK
        if gc_hash == GC_HASH_HASFIELD:
            obj = llarena.getfakearenaaddress(obj)
            return (obj + objsize).signed[0]
        elif gc_hash == GC_HASH_TAKEN_ADDR:
            return llmemory.cast_adr_to_int(obj)
        elif gc_hash == GC_HASH_TAKEN_NURS:
            return self._compute_current_nursery_hash(obj)
        else:
            assert 0, "gc_hash == GC_HASH_NOTTAKEN"

    def _make_a_copy_with_tid(self, obj, objsize, tid):
        return NotImplemented

    def make_a_copy(self, obj, objsize):
        tid = self.header(obj).tid
        return self._make_a_copy_with_tid(obj, objsize, tid)

    def trace_and_copy(self, obj):
        self.trace(obj, self.make_callback('_trace_copy'), self, None)

    def _trace_copy(self, pointer, ignored):
        pointer.address[0] = self.copy(pointer.address[0])

    def surviving(self, obj):
        return NotImplemented

    def is_forwarded(self, obj):
        return self.header(obj).tid & GCFLAG_FORWARDED != 0

    def get_forwarding_address(self, obj):
        return NotImplemented

    def visit_external_object(self, obj):
        pass

    def get_possibly_forwarded_type_id(self, obj):
        tid = self.header(obj).tid
        if self.is_forwarded(obj) and not (tid & GCFLAG_EXTERNAL):
            obj = self.get_forwarding_address(obj)
        return self.get_type_id(obj)

    def set_forwarding_address(self, obj, newobj, objsize):
        return NotImplemented

    def combine(self, typeid16, flags):
        return llop.combine_ushort(lltype.Signed, typeid16, flags)

    def get_type_id(self, addr):
        return NotImplemented

    def init_gc_object(self, addr, typeid16, flags=0):
        hdr = llmemory.cast_adr_to_ptr(addr, lltype.Ptr(self.HDR))
        hdr.tid = self.combine(typeid16, flags)

    def init_gc_object_immortal(self, addr, typeid16, flags=0):
        hdr = llmemory.cast_adr_to_ptr(addr, lltype.Ptr(self.HDR))
        flags |= GCFLAG_EXTERNAL | GCFLAG_FORWARDED | GC_HASH_TAKEN_ADDR
        hdr.tid = self.combine(typeid16, flags)

    def deal_with_objects_with_light_finalizers(self):
        return NotImplemented

    def deal_with_objects_with_finalizers(self, scan):
        return NotImplemented

    def _append_if_nonnull(pointer, stack, ignored):
        stack.append(pointer.address[0])
    _append_if_nonnull = staticmethod(_append_if_nonnull)

    def _finalization_state(self, obj):
        if self.surviving(obj):
            newobj = self.get_forwarding_address(obj)
            hdr = self.header(newobj)
            if hdr.tid & GCFLAG_FINALIZATION_ORDERING:
                return 2
            else:
                return 3
        else:
            hdr = self.header(obj)
            if hdr.tid & GCFLAG_FINALIZATION_ORDERING:
                return 1
            else:
                return 0

    def _bump_finalization_state_from_0_to_1(self, obj):
        ll_assert(self._finalization_state(obj) == 0,
                  "unexpected finalization state != 0")
        hdr = self.header(obj)
        hdr.tid |= GCFLAG_FINALIZATION_ORDERING

    def _recursively_bump_finalization_state_from_2_to_3(self, obj):
        ll_assert(self._finalization_state(obj) == 2,
                  "unexpected finalization state != 2")
        newobj = self.get_forwarding_address(obj)
        pending = self.tmpstack
        ll_assert(not pending.non_empty(), "tmpstack not empty")
        pending.append(newobj)
        while pending.non_empty():
            y = pending.pop()
            hdr = self.header(y)
            if hdr.tid & GCFLAG_FINALIZATION_ORDERING:     # state 2 ?
                hdr.tid &= ~GCFLAG_FINALIZATION_ORDERING   # change to state 3
                self.trace(y, self._append_if_nonnull, pending, None)

    def _recursively_bump_finalization_state_from_1_to_2(self, obj, scan):
        # recursively convert objects from state 1 to state 2.
        # Note that copy() copies all bits, including the
        # GCFLAG_FINALIZATION_ORDERING.  The mapping between
        # state numbers and the presence of this bit was designed
        # for the following to work :-)
        self.copy(obj)
        return self.scan_copied(scan)

    def invalidate_weakrefs(self):
        # walk over list of objects that contain weakrefs
        # if the object it references survives then update the weakref
        # otherwise invalidate the weakref
        new_with_weakref = self.AddressStack()
        while self.objects_with_weakrefs.non_empty():
            obj = self.objects_with_weakrefs.pop()
            if not self.surviving(obj):
                continue # weakref itself dies
            obj = self.get_forwarding_address(obj)
            offset = self.weakpointer_offset(self.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            # XXX I think that pointing_to cannot be NULL here
            if pointing_to:
                if self.surviving(pointing_to):
                    (obj + offset).address[0] = self.get_forwarding_address(
                        pointing_to)
                    new_with_weakref.append(obj)
                else:
                    (obj + offset).address[0] = NULL
        self.objects_with_weakrefs.delete()
        self.objects_with_weakrefs = new_with_weakref

    def _is_external(self, obj):
        return (self.header(obj).tid & GCFLAG_EXTERNAL) != 0

    def _is_in_the_space(self, obj):
        return self.tospace <= obj < self.free

    def debug_check_object(self, obj):
        """Check the invariants about 'obj' that should be true
        between collections."""
        tid = self.header(obj).tid
        if tid & GCFLAG_EXTERNAL:
            ll_assert(tid & GCFLAG_FORWARDED != 0, "bug: external+!forwarded")
            ll_assert(not (self.tospace <= obj < self.free),
                      "external flag but object inside the semispaces")
        else:
            ll_assert(not (tid & GCFLAG_FORWARDED), "bug: !external+forwarded")
            ll_assert(self.tospace <= obj < self.free,
                      "!external flag but object outside the semispaces")
        ll_assert(not (tid & GCFLAG_FINALIZATION_ORDERING),
                  "unexpected GCFLAG_FINALIZATION_ORDERING")

    def debug_check_can_copy(self, obj):
        return NotImplemented

    STATISTICS_NUMBERS = 0

    def is_in_nursery(self, addr):
        # overridden in generation.py.
        return False

    def _compute_current_nursery_hash(self, obj):
        # overridden in generation.py.
        raise AssertionError("should not be called")

    def identityhash(self, gcobj):
        return NotImplemented

    def track_heap_parent(self, obj, parent):
        addr = obj.address[0]
        return NotImplemented

    def track_heap(self, adr):
        return NotImplemented

    @staticmethod
    def _track_heap_root(obj, self):
        return NotImplemented

    def heap_stats(self):
        return NotImplemented
