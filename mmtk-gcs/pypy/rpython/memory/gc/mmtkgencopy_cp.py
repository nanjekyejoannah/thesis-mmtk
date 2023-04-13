import sys
from rpython.rtyper.lltypesystem import lltype, llmemory, llarena, llgroup
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rtyper.lltypesystem.llmemory import raw_malloc_usage
from rpython.memory.gc.base import GCBase, MovingGCBase
from rpython.memory.gc import env
from rpython.memory.support import mangle_hash
from rpython.rlib.rarithmetic import ovfcheck, LONG_BIT, intmask, r_uint
from rpython.rlib.rarithmetic import LONG_BIT_SHIFT
from rpython.rlib.debug import ll_assert, debug_print, debug_start, debug_stop
from rpython.rlib.objectmodel import specialize

WORD = LONG_BIT // 8
NULL = llmemory.NULL

first_gcflag = 1 << (LONG_BIT//2)
GCFLAG_TRACK_YOUNG_PTRS = first_gcflag << 0
GCFLAG_NO_HEAP_PTRS = first_gcflag << 1
GCFLAG_VISITED      = first_gcflag << 2
GCFLAG_HAS_SHADOW   = first_gcflag << 3
GCFLAG_FINALIZATION_ORDERING = first_gcflag << 4
GCFLAG_EXTRA        = first_gcflag << 5
GCFLAG_HAS_CARDS    = first_gcflag << 6
GCFLAG_CARDS_SET    = first_gcflag << 7
GCFLAG_DUMMY        = first_gcflag << 8

_GCFLAG_FIRST_UNUSED = first_gcflag << 9


FORWARDSTUB = lltype.GcStruct('forwarding_stub',
                              ('forw', llmemory.Address))
FORWARDSTUBPTR = lltype.Ptr(FORWARDSTUB)
NURSARRAY = lltype.Array(llmemory.Address)

# ____________________________________________________________

#init
ll_mmtk_gc_init = llexternal_mmtk('pypy_gc_init', [], lltype.Void)
ll_mmtk_is_gc_initialized = llexternal_mmtk('pypy_is_gc_initialized', [], lltype.Bool)
ll_mmtk_mmtk_set_heap_size = llexternal_mmtk('mmtk_set_heap_size', [rffi.SIGNED, rffi.SIGNED], lltype.Bool)

#mutator
ll_mmtk_bind_mutator = llexternal_mmtk('bind_mutator', [], rffi.VOIDP)
ll_mmtk_destroy_mutator = llexternal_mmtk('destroy_mutator', [rffi.VOIDP], lltype.Void)
ll_mmtk_flush_mutator = llexternal_mmtk('flush_mutator', [rffi.VOIDP], lltype.Void)

#allocation
ll_mmtk_alloc = llexternal_mmtk('mmtk_alloc', [rffi.VOIDP, rffi.SIGNED, rffi.SIGNED, rffi.SIGNED], lltype.Unsigned)

#stats
ll_mmtk_free_bytes  = llexternal_mmtk('mmtk_free_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_total_bytes = llexternal_mmtk('mmtk_total_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_used_bytes = llexternal_mmtk('mmtk_used_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_starting_heap_address = llexternal_mmtk('mmtk_starting_heap_address', [rffi.VOID], lltype.VoidP)
ll_mmtk_last_heap_address = llexternal_mmtk('mmtk_last_heap_address ', [rffi.VOID], lltype.VoidP)

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

#writebarrier
ll_mmtk_object_reference_write_pre  = llexternal_mmtk('object_reference_write_pre', [rffi.VOIDP], lltype.Void)
ll_mmtk_object_reference_write_post  = llexternal_mmtk('object_reference_write_post', [rffi.VOIDP], lltype.Void)
ll_mmtk_object_reference_write_slow  = llexternal_mmtk('object_reference_write_slow', [rffi.VOIDP], lltype.Void)
ll_mmtk_array_copy_pre  = llexternal_mmtk('array_copy_pre', [rffi.VOIDP], lltype.Void)
ll_mmtk_array_copy_post  = llexternal_mmtk('array_copy_post', [rffi.VOIDP], lltype.Void)

class MMTKGenCopyGC(MovingGCBase):
    inline_simple_malloc = True
    inline_simple_malloc_varsize = True
    needs_write_barrier = True
    prebuilt_gc_objects_are_static_roots = False
    malloc_zero_filled = True 
    gcflag_extra = GCFLAG_EXTRA
    gcflag_dummy = GCFLAG_DUMMY

    HDR = lltype.Struct('header', ('tid', lltype.Signed))
    typeid_is_in_field = 'tid'

    _ADDRARRAY = lltype.Array(llmemory.Address, hints={'nolength': True})


    minimal_size_in_nursery = (
        llmemory.sizeof(HDR) + llmemory.sizeof(llmemory.Address))


    TRANSLATION_PARAMS = {
        "read_from_env": True,
        "nursery_size": 896*1024,
        "page_size": 1024*WORD,
        "arena_size": 65536*WORD,
        "small_request_threshold": 35*WORD,
        "major_collection_threshold": 1.82,
        "growth_rate_max": 1.4,
        "card_page_indices": 128,
        "large_object": (16384+512)*WORD,
        "nursery_cleanup": 32768 * WORD,
        }

    def __init__(self, config,
                 read_from_env=False,
                 nursery_size=32*WORD,
                 nursery_cleanup=9*WORD,
                 page_size=16*WORD,
                 arena_size=64*WORD,
                 small_request_threshold=5*WORD,
                 major_collection_threshold=2.5,
                 growth_rate_max=2.5,   # for tests
                 card_page_indices=0,
                 large_object=8*WORD,
                 ArenaCollectionClass=None,
                 **kwds):
        MovingGCBase.__init__(self, config, **kwds)
        assert small_request_threshold % WORD == 0
        self.read_from_env = read_from_env
        self.small_request_threshold = small_request_threshold
        self.major_collection_threshold = major_collection_threshold
        self.growth_rate_max = growth_rate_max
        self.num_major_collects = 0
        self.min_heap_size = 0.0
        self.max_heap_size = 0.0
        self.max_heap_size_already_raised = False
        self.max_delta = float(r_uint(-1))
        self.nonlarge_max = large_object - 1
        self.extra_threshold = 0

        self.old_objects_pointing_to_young = self.AddressStack()
        self.old_objects_with_cards_set = self.AddressStack()
        self.prebuilt_root_objects = self.AddressStack()
        self._init_writebarrier_logic()

        ll_mmtk_gc_init() 
        ll_assert(ll_mmtk_is_gc_initialized() == True, 
        "MMTK initialized successfully")
        ll_assert(ll_mmtk_mmtk_set_heap_size(self.param_space_size, self.param_max_space_size)  == True,
        "MMTK heap size set successfully")
        self.handle = ll_mmtk_bind_mutator()


    def setup(self):
        """Called at run-time to initialize the GC."""
        
        GCBase.setup(self)
        
        p = lltype.malloc(self._ADDRARRAY, 1, flavor='raw',
                          track_allocation=False)
        self.singleaddr = llmemory.cast_ptr_to_adr(p)
        
        if not self.read_from_env:
            pass
        else:
            #
            defaultsize = self.nursery_size
            minsize = 2 * (self.nonlarge_max + 1)
            
            min_heap_size = env.read_uint_from_env('PYPY_GC_MIN')
            if min_heap_size > 0:
                self.min_heap_size = float(min_heap_size)
            else:
                self.min_heap_size = newsize * 8
            #
            max_heap_size = env.read_uint_from_env('PYPY_GC_MAX')
            if max_heap_size > 0:
                self.max_heap_size = float(max_heap_size)
            #
            max_delta = env.read_uint_from_env('PYPY_GC_MAX_DELTA')
            if max_delta > 0:
                self.max_delta = float(max_delta)
            else:
                self.max_delta = 0.125 * env.get_total_memory()


    def set_major_threshold_from(self, threshold, reserving_size=0):
        threshold_max = (self.next_major_collection_initial *
                         self.growth_rate_max)
        if threshold > threshold_max:
            threshold = threshold_max
        #
        threshold += reserving_size
        if threshold < self.min_heap_size:
            threshold = self.min_heap_size
        #
        if self.max_heap_size > 0.0 and threshold > self.max_heap_size:
            threshold = self.max_heap_size
            bounded = True
        else:
            bounded = False
        #
        self.next_major_collection_initial = threshold
        self.next_major_collection_threshold = threshold
        return bounded


    def post_setup(self):
        MovingGCBase.post_setup(self)

    def debug_rotate_nursery(self):
        return NotImplemented

    def malloc_fixedsize_clear(self, typeid, size,
                               needs_finalizer=False,
                               is_finalizer_light=False,
                               contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + size
        rawtotalsize = raw_malloc_usage(totalsize)
        
        if needs_finalizer and not is_finalizer_light:
            ll_assert(not contains_weakptr,
                     "'needs_finalizer' and 'contains_weakptr' both specified")
            #obj = self.external_malloc(typeid, 0, alloc_young=False)
            addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
            obj = llmemory.cast_ptr_to_adr(addr_ptr)
            #res = llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)
            res = llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)
            self.register_finalizer(-1, res)
            return res
            
        if rawtotalsize > self.nonlarge_max:
            ll_assert(not contains_weakptr,
                      "'contains_weakptr' specified for a large object")
            #obj = self.external_malloc(typeid, 0, alloc_young=True)
            addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
            obj = llmemory.cast_ptr_to_adr(addr_ptr)
        else:
            min_size = raw_malloc_usage(self.minimal_size_in_nursery)
            if rawtotalsize < min_size:
                totalsize = rawtotalsize = min_size
            #result = self.nursery_free
            addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
            result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
            llarena.arena_reserve(result, totalsize)
            obj = result + size_gc_header
            self.init_gc_object(result, typeid, flags=0)
        if needs_finalizer:
            self.young_objects_with_destructors.append(obj)
            ll_mmtk_add_finalizer(llmemory.cast_adr_to_ptr(obj, llmemory.GCREF))
        if contains_weakptr:
            # self.young_objects_with_weakrefs.append(obj)
            ll_mmtk_add_weak_candidate(llmemory.cast_adr_to_ptr(obj, llmemory.GCREF))
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def malloc_varsize_clear(self, typeid, length, size, itemsize,
                             offset_to_length):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        nonvarsize = size_gc_header + size
        maxsize = self.nonlarge_max - raw_malloc_usage(nonvarsize)
        if maxsize < 0:
            toobig = r_uint(0)
        elif raw_malloc_usage(itemsize):
            toobig = r_uint(maxsize // raw_malloc_usage(itemsize)) + 1
        else:
            toobig = r_uint(sys.maxint) + 1

        if r_uint(length) >= r_uint(toobig):
            # obj = self.external_malloc(typeid, length, alloc_young=True)
            addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
            obj = llmemory.cast_ptr_to_adr(addr_ptr)
        else:
            totalsize = nonvarsize + itemsize * length
            totalsize = llarena.round_up_for_allocation(totalsize)
            ll_assert(raw_malloc_usage(totalsize) >=
                      raw_malloc_usage(self.minimal_size_in_nursery),
                      "malloc_varsize_clear(): totalsize < minimalsize")
            # result = self.nursery_free
            addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
            result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
            llarena.arena_reserve(result, totalsize)
            self.init_gc_object(result, typeid, flags=0)
            obj = result + size_gc_header
            (obj + offset_to_length).signed[0] = length
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def malloc_fixed_or_varsize_nonmovable(self, typeid, length):
        # obj = self.external_malloc(typeid, length, alloc_young=True)
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        obj = llmemory.cast_ptr_to_adr(addr_ptr)
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def collect(self, gen=1):
        return NotImplemented

    def move_nursery_top(self, totalsize):
        return NotImplemented
    move_nursery_top._always_inline_ = True

    def collect_and_reserve(self, prev_result, totalsize):
        return NotImplemented
    collect_and_reserve._dont_inline_ = True


    # ----------
    # Other functions in the GC API

    def set_max_heap_size(self, size):
        self.max_heap_size = float(size)
        ll_mmtk_mmtk_set_heap_size(0, self.max_heap_size)

    def raw_malloc_memory_pressure(self, sizehint, adr):
        return NotImplemented

    def can_optimize_clean_setarrayitems(self):
        return MovingGCBase.can_optimize_clean_setarrayitems(self)

    def can_move(self, obj):
        """Overrides the parent can_move()."""
        return ll_mmtk_will_never_move(llmemory.cast_adr_to_ptr(obj, llmemory.GCREF))


    def shrink_array(self, obj, smallerlength):
        if self.header(obj).tid & GCFLAG_HAS_SHADOW:
            return False
        #
        size_gc_header = self.gcheaderbuilder.size_gc_header
        typeid = self.get_type_id(obj)
        totalsmallersize = (
            size_gc_header + self.fixed_size(typeid) +
            self.varsize_item_sizes(typeid) * smallerlength)
        llarena.arena_shrink_obj(obj - size_gc_header, totalsmallersize)
        #
        offset_to_length = self.varsize_offset_to_length(typeid)
        (obj + offset_to_length).signed[0] = smallerlength
        return True


    # ----------
    # Simple helpers

    def get_type_id(self, obj):
        tid = self.header(obj).tid
        return llop.extract_ushort(llgroup.HALFWORD, tid)

    def combine(self, typeid16, flags):
        return llop.combine_ushort(lltype.Signed, typeid16, flags)

    def init_gc_object(self, addr, typeid16, flags=0):
        hdr = llmemory.cast_adr_to_ptr(addr, lltype.Ptr(self.HDR))
        hdr.tid = self.combine(typeid16, flags)

    def init_gc_object_immortal(self, addr, typeid16, flags=0):
        flags |= GCFLAG_NO_HEAP_PTRS | GCFLAG_TRACK_YOUNG_PTRS
        self.init_gc_object(addr, typeid16, flags)

    def is_in_nursery(self, addr):
        return NotImplemented

    def appears_to_be_young(self, addr):
        return NotImplemented
    appears_to_be_young._always_inline_ = True

    def debug_is_old_object(self, addr):
        return NotImplemented

    def is_forwarded(self, obj):
        return NotImplemented

    def get_forwarding_address(self, obj):
        return llmemory.cast_adr_to_ptr(obj, FORWARDSTUBPTR).forw

    def get_possibly_forwarded_type_id(self, obj):
        return NotImplemented

    def get_total_memory_used(self):
        """Return the total memory used, not counting any object in the
        nursery: only objects in the ArenaCollection or raw-malloced.
        """
        return self.ac.total_memory_used + self.rawmalloced_total_size

    def card_marking_words_for_length(self, length):
        return intmask(
          ((r_uint(length) + r_uint((LONG_BIT << self.card_page_shift) - 1)) >>
           (self.card_page_shift + LONG_BIT_SHIFT)))

    def card_marking_bytes_for_length(self, length):
        return intmask(
            ((r_uint(length) + r_uint((8 << self.card_page_shift) - 1)) >>
             (self.card_page_shift + 3)))

    def debug_check_consistency(self):
        if self.DEBUG:
            MovingGCBase.debug_check_consistency(self)

    def debug_check_object(self, obj):
        return NotImplemented

    
    JIT_WB_IF_FLAG = GCFLAG_TRACK_YOUNG_PTRS

    if TRANSLATION_PARAMS['card_page_indices'] > 0:
        JIT_WB_CARDS_SET = GCFLAG_CARDS_SET
        JIT_WB_CARD_PAGE_SHIFT = 1
        while ((1 << JIT_WB_CARD_PAGE_SHIFT) !=
               TRANSLATION_PARAMS['card_page_indices']):
            JIT_WB_CARD_PAGE_SHIFT += 1

    @classmethod
    def JIT_max_size_of_young_obj(cls):
        return cls.TRANSLATION_PARAMS['large_object']

    @classmethod
    def JIT_minimal_size_in_nursery(cls):
        return cls.minimal_size_in_nursery

    def write_barrier(self, addr_struct):
        if self.header(addr_struct).tid:
            ll_mmtk_object_reference_write_pre(addr_struct)
            ll_mmtk_object_reference_write_slow(addr_struct)
            ll_mmtk_object_reference_write_post(addr_struct)

    def write_barrier_from_array(self, addr_array, index):
        if self.header(addr_array).tid:
            ll_mmtk_array_copy_pre(addr_array)
            ll_mmtk_array_copy_post(addr_array)

    def _init_writebarrier_logic(self):
        return NotImplemented

    def get_card(self, obj, byteindex):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        addr_byte = obj - size_gc_header
        return llarena.getfakearenaaddress(addr_byte) + (~byteindex)


    def writebarrier_before_copy(self, source_addr, dest_addr,
                                 source_start, dest_start, length):
        """ This has the same effect as calling writebarrier over
        each element in dest copied from source, except it might reset
        one of the following flags a bit too eagerly, which means we'll have
        a bit more objects to track, but being on the safe side.
        """
        source_hdr = self.header(source_addr)
        dest_hdr = self.header(dest_addr)
        return True

    def writebarrier_before_move(self, array_addr):
        """If 'array_addr' uses cards, then this has the same effect as
        a call to the generic writebarrier, effectively generalizing the
        cards to "any item may be young".
        """
        if self.card_page_indices <= 0:     # check constant-folded
            return     # no cards, nothing to do
        #
        array_hdr = self.header(array_addr)
        if array_hdr.tid:
            self.write_barrier(array_addr)

    def manually_copy_card_bits(self, source_addr, dest_addr, length):
        # manually copy the individual card marks from source to dest
        assert self.card_page_indices > 0
        bytes = self.card_marking_bytes_for_length(length)
        #
        return NotImplemented

    def register_finalizer(self, fq_index, gcobj):
        from rpython.rtyper.lltypesystem import rffi
        obj = llmemory.cast_ptr_to_adr(gcobj)
        fq_index = rffi.cast(llmemory.Address, fq_index)
        ll_mmtk_add_finalizer(gcobj)

    def trace_and_drag_out_of_nursery(self, obj):
        """obj must not be in the nursery.  This copies all the
        young objects it references out of the nursery.
        """
        return NotImplemented

    def trace_and_drag_out_of_nursery_partial(self, obj, start, stop):
        """Like trace_and_drag_out_of_nursery(), but limited to the array
        indices in range(start, stop).
        """
        return NotImplemented


    # ----------
    # id() and identityhash() support

    def _allocate_shadow(self, obj):
        return NotImplemented

    def _find_shadow(self, obj):
        #
        return NotImplemented
    _find_shadow._dont_inline_ = True

    def id_or_identityhash(self, gcobj):
        """Implement the common logic of id() and identityhash()
        of an object, given as a GCREF.
        """
        obj = llmemory.cast_ptr_to_adr(gcobj)
        return llmemory.cast_adr_to_int(obj)
    id_or_identityhash._always_inline_ = True

    def id(self, gcobj):
        return self.id_or_identityhash(gcobj)

    def identityhash(self, gcobj):
        return mangle_hash(self.id_or_identityhash(gcobj))

    # ----------
    # Finalizers

    def deal_with_objects_with_finalizers(self):
        return NotImplemented

    def _append_if_nonnull(pointer, stack):
        stack.append(pointer.address[0])
    _append_if_nonnull = staticmethod(_append_if_nonnull)

    def _finalization_state(self, obj):
        tid = self.header(obj).tid
        if tid & GCFLAG_VISITED:
            if tid & GCFLAG_FINALIZATION_ORDERING:
                return 2
            else:
                return 3
        else:
            if tid & GCFLAG_FINALIZATION_ORDERING:
                return 1
            else:
                return 0

    def _bump_finalization_state_from_0_to_1(self, obj):
       return NotImplemented

    def _recursively_bump_finalization_state_from_2_to_3(self, obj):
        return NotImplemented

    def _recursively_bump_finalization_state_from_1_to_2(self, obj):
        return NotImplemented

    def invalidate_young_weakrefs(self):
        return NotImplemented

    def invalidate_old_weakrefs(self):
        return NotImplemented
