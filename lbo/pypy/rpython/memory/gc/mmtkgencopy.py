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

#init
# Just duplication, no semantics
ll_mmtk_pypy_gc_init = llexternal_mmtk('pypy_gc_init', [rffi.VOIDP, rffi.INT], lltype.Void)
ll_mmtk_initialize_collection = llexternal_mmtk('mmtk_initialize_collection(', [rffi.VOID], lltype.Void)
ll_mmtk_enable_collection = llexternal_mmtk('mmtk_enable_collection', [rffi.VOID], lltype.Void)
ll_mmtk_start_control_collector = llexternal_mmtk('mmtk_start_control_collector', [rffi.VOID], lltype.Void)
ll_mmtk_start_worker = llexternal_mmtk('pypy_get_mutator_thread', [rffi.VOID, rffi.VOID], lltype.Void)

ll_mmtk_pypy_get_mutator_thread = llexternal_mmtk('pypy_get_mutator_thread', [rffi.VOID], lltype.Void)
ll_mmtk_bind_mutator = llexternal_mmtk('mmtk_bind_mutator', [rffi.VOIDP], lltype.VoidP)
ll_mmtk_destroy_mutator = llexternal_mmtk('mmtk_destroy_mutator', [rffi.VOIDP], lltype.Void)

#allocation
ll_mmtk_alloc = llexternal_mmtk('mmtk_alloc', [rffi.VOIDP, rffi.INT, rffi.INT, rffi.INT, rffi.INT], lltype.VoidP)
ll_mmtk_post_alloc = llexternal_mmtk('mmtk_post_alloc', [rffi.VOIDP, rffi.INT, rffi.INT, rffi.INT, rffi.INT], lltype.Void)

#scan
ll_mmtk_is_live_object = llexternal_mmtk('mmtk_is_live_object', [rffi.VOIDP], lltype.Void)
ll_mmtk_is_mmtk_object = llexternal_mmtk('mmtk_is_mmtk_object ', [rffi.VOIDP], lltype.VoidP)
ll_mmtk_is_mmtk_object_prechecked = llexternal_mmtk('mmtk_is_mmtk_object_prechecked ', [rffi.VOIDP], lltype.VoidP)
ll_mmtk_modify_check = llexternal_mmtk('mmtk_modify_check ', [rffi.VOIDP], lltype.Void)
ll_mmtk_flush_mark_buffer = llexternal_mmtk('mmtk_flush_mark_buffer', [rffi.VOIDP], lltype.Void)

# collect etc
ll_mmtk_will_never_move = llexternal_mmtk('mmtk_will_never_move ', [rffi.VOIDP], lltype.VoidP)
ll_mmtk_handle_user_collection_request = llexternal_mmtk('mmtk_handle_user_collection_request', [rffi.VOIDP], lltype.Void)
ll_mmtk_plan_name = llexternal_mmtk('mmtk_plan_name ', [rffi.VOID], lltype.VoidP)

#stats
ll_mmtk_free_bytes  = llexternal_mmtk('mmtk_free_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_total_bytes = llexternal_mmtk('mmtk_total_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_used_bytes = llexternal_mmtk('mmtk_used_bytes', [rffi.VOID], rffi.INT)
ll_mmtk_starting_heap_address = llexternal_mmtk('mmtk_starting_heap_address', [rffi.VOID], lltype.VoidP)
ll_mmtk_last_heap_address = llexternal_mmtk('mmtk_last_heap_address ', [rffi.VOID], lltype.VoidP)

# finalizer
ll_mmtk_add_finalizer  = llexternal_mmtk('add_finalizer', [rffi.VOIDP], lltype.Void)
ll_mmtk_get_finalized_object  = llexternal_mmtk('get_finalized_object', [], lltype.Void)

# weakref
ll_mmtk_add_weakref  = llexternal_mmtk('add_weak_candidate', [rffi.VOIDP], lltype.Void)

#writebarrier
ll_mmtk_object_reference_write_pre  = llexternal_mmtk('object_reference_write_pre ', [rffi.VOIDP], lltype.Void)
ll_mmtk_object_reference_write_post  = llexternal_mmtk('object_reference_write_post', [rffi.VOIDP], lltype.Void)
ll_mmtk_object_reference_write_slow  = llexternal_mmtk('object_reference_write_slow', [rffi.VOIDP], lltype.Void)
ll_mmtk_array_copy_pre  = llexternal_mmtk('array_copy_pre', [rffi.VOIDP], lltype.Void)
ll_mmtk_array_copy_post  = llexternal_mmtk('array_copy_post', [rffi.VOIDP], lltype.Void)

# ____________________________________________________________

class MMTKGenCopyGC(MovingGCBase):
    _alloc_flavor_ = "raw"
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
        self.max_heap_size = env.read_uint_from_env('PYPY_GC_MAX')
        ll_mmtk_pypy_gc_init(lltype.nullptr(self.HDR), self.max_heap_size)
        self.vm_thread_mutator = ll_mmtk_pypy_get_mutator_thread()
        self.mutator = ll_mmtk_mmtk_bind_mutator(self.vm_thread_mutator)
        ll_mmtk_enable_collection()
        ll_mmtk_initialize_collection()
        ll_mmtk_start_control_collector()
        ll_mmtk_start_worker()
        self.prebuilt_root_objects = self.AddressStack()
        self._init_writebarrier_logic()


    def setup(self):
        """Called at run-time to initialize the GC."""
        GCBase.setup(self)

        #finalizers
        self.old_objects_with_finalizers = ll_mmtk_get_finalized_object()
        
        # mmtk no op for these
        # Why dont we expose this?
        self.young_objects_with_weakrefs = []
        self.old_objects_with_weakrefs = []

    #we may have to give these configs
    def set_major_threshold_from(self, threshold, reserving_size=0):
        pass

    def post_setup(self):
        MovingGCBase.post_setup(self)

    def malloc_fixedsize_clear(self, typeid, size,
                               needs_finalizer=False,
                               is_finalizer_light=False,
                               contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + size
        rawtotalsize = raw_malloc_usage(totalsize)
        
        if needs_finalizer and not is_finalizer_light:
            # old-style finalizers only!
            ll_assert(not contains_weakptr,
                     "'needs_finalizer' and 'contains_weakptr' both specified")
            obj = self.external_malloc(typeid, 0, alloc_young=False)
            res = llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)
            self.register_finalizer(-1, res)
            return res

        result = llmemory.cast_adr_to_int (ll_mmtk_alloc(self.mutator, totalsize, typeid16, size_gc_header, 0))
        obj = result + size_gc_header
        self.init_gc_object(result, typeid, flags=0)
       
        if needs_finalizer:
            ll_mmtk_add_finalizer(obj)
        if contains_weakptr:
            ll_mmtk_add_weakref(obj)
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def malloc_varsize_clear(self, typeid, length, size, itemsize,
                             offset_to_length):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        nonvarsize = size_gc_header + size
        
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + size
        rawtotalsize = raw_malloc_usage(totalsize)
        
        if needs_finalizer and not is_finalizer_light:
            ll_assert(not contains_weakptr,
                     "'needs_finalizer' and 'contains_weakptr' both specified")
            obj = self.external_malloc(typeid, 0, alloc_young=False)
            res = llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)
            self.register_finalizer(-1, res)
            return res

        result = llmemory.cast_adr_to_int (ll_mmtk_alloc(self.mutator, totalsize, typeid16, size_gc_header, 0))
        obj = result + size_gc_header
        self.init_gc_object(result, typeid, flags=0)
       
        if needs_finalizer:
            ll_mmtk_add_finalizer(obj)
        if contains_weakptr:
            ll_mmtk_add_weakref(obj)
        return llmemory.cast_adr_to_ptr(obj, llmemory.GCREF)


    def malloc_fixed_or_varsize_nonmovable(self, typeid, length):
        pass


    def collect(self, gen=1):
        pass


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
        ll_assert(llmemory.cast_adr_to_int(addr) & 1 == 0,
                  "odd-valued (i.e. tagged) pointer unexpected here")
        pass

    def debug_is_old_object(self, addr):
        pass

    def get_forwarding_address(self, obj):
        return llmemory.cast_adr_to_ptr(obj, FORWARDSTUBPTR).forw

    def get_possibly_forwarded_type_id(self, obj):
        if self.is_in_nursery(obj) and self.is_forwarded(obj):
            obj = self.get_forwarding_address(obj)
        return self.get_type_id(obj)

    def get_total_memory_used(self):
        """Return the total memory used, not counting any object in the
        nursery: only objects in the ArenaCollection or raw-malloced.
        """
        pass

    def debug_check_consistency(self):
        if self.DEBUG:
            MovingGCBase.debug_check_consistency(self)

    def debug_check_object(self, obj):
        ll_assert(not self.is_in_nursery(obj),
                  "object in nursery after collection")
        typeid = self.get_type_id(obj)
        if self.has_gcptr(typeid):
            ll_assert(self.header(obj).tid & GCFLAG_TRACK_YOUNG_PTRS != 0,
                      "missing GCFLAG_TRACK_YOUNG_PTRS")

    # ----------
    # Write barrier

    @classmethod
    def JIT_max_size_of_young_obj(cls):
        return cls.TRANSLATION_PARAMS['large_object']

    @classmethod
    def JIT_minimal_size_in_nursery(cls):
        return cls.minimal_size_in_nursery

    def write_barrier(self, addr_struct):
        if self.header(addr_struct).tid & GCFLAG_TRACK_YOUNG_PTRS:
            ll_mmtk_object_reference_write_pre(addr_struct)
            ll_mmtk_object_reference_write_slow(addr_struct)
            ll_mmtk_object_reference_write_post(addr_struct)

    def write_barrier_from_array(self, addr_array, index):
        if self.header(addr_array).tid & GCFLAG_TRACK_YOUNG_PTRS:
            ll_mmtk_array_copy_pre(addr_array)
            ll_mmtk_array_copy_post(addr_array)

    def _init_writebarrier_logic(self):
        DEBUG = self.DEBUG
        def remember_young_pointer(addr_struct):
            if DEBUG:   # note: PYPY_GC_DEBUG=1 does not enable this
                ll_assert(self.debug_is_old_object(addr_struct) or
                          self.header(addr_struct).tid & GCFLAG_HAS_CARDS != 0,
                      "young object with GCFLAG_TRACK_YOUNG_PTRS and no cards")

    def register_finalizer(self, fq_index, gcobj):
        from rpython.rtyper.lltypesystem import rffi
        obj = llmemory.cast_ptr_to_adr(gcobj)
        fq_index = rffi.cast(llmemory.Address, fq_index)
        ll_mmtk_add_finalizer(obj)

    def collect_roots(self):
        # Collect all roots.  Starts from all the objects
        # from 'prebuilt_root_objects'.
        self.prebuilt_root_objects.foreach(self._collect_obj,
                                           self.objects_to_trace)
        #
        # Add the roots from the other sources.
        self.root_walker.walk_roots(
            MiniMarkGC._collect_ref_stk, # stack roots
            MiniMarkGC._collect_ref_stk, # static in prebuilt non-gc structures
            None)   # we don't need the static in all prebuilt gc objects
        #
        # If we are in an inner collection caused by a call to a finalizer,
        # the 'run_finalizers' objects also need to be kept alive.
        self.enum_pending_finalizers(self._collect_obj,
                                     self.objects_to_trace)

    def enumerate_all_roots(self, callback, arg):
        self.prebuilt_root_objects.foreach(callback, arg)
        MovingGCBase.enumerate_all_roots(self, callback, arg)
    enumerate_all_roots._annspecialcase_ = 'specialize:arg(1)'

    def enum_live_with_finalizers(self, callback, arg):
        self.probably_young_objects_with_finalizers.foreach(callback, arg, 2)
        self.old_objects_with_finalizers.foreach(callback, arg, 2)
    enum_live_with_finalizers._annspecialcase_ = 'specialize:arg(1)'

    @staticmethod
    def _collect_obj(obj, objects_to_trace):
        objects_to_trace.append(obj)

    def _collect_ref_stk(self, root):
        obj = root.address[0]
        llop.debug_nonnull_pointer(lltype.Void, obj)
        self.objects_to_trace.append(obj)

    def _collect_ref_rec(self, root, ignored):
        self.objects_to_trace.append(root.address[0])

    def visit_all_objects(self):
        pending = self.objects_to_trace
        while pending.non_empty():
            obj = pending.pop()
            self.visit(obj)

    def visit(self, obj):
        #
        # 'obj' is a live object.  Check GCFLAG_VISITED to know if we
        # have already seen it before.
        #
        # Moreover, we can ignore prebuilt objects with GCFLAG_NO_HEAP_PTRS.
        # If they have this flag set, then they cannot point to heap
        # objects, so ignoring them is fine.  If they don't have this
        # flag set, then the object should be in 'prebuilt_root_objects',
        # and the GCFLAG_VISITED will be reset at the end of the
        # collection.
        hdr = self.header(obj)
        if hdr.tid & (GCFLAG_VISITED | GCFLAG_NO_HEAP_PTRS):
            return
        #
        # It's the first time.  We set the flag.
        hdr.tid |= GCFLAG_VISITED
        if not self.has_gcptr(llop.extract_ushort(llgroup.HALFWORD, hdr.tid)):
            return
        #
        # Trace the content of the object and put all objects it references
        # into the 'objects_to_trace' list.
        self.trace(obj, self.make_callback('_collect_ref_rec'), self, None)


    # ----------
    # id() and identityhash() support

    def _allocate_shadow(self, obj):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        size = self.get_size(obj)
        shadowhdr = self._malloc_out_of_nursery(size_gc_header +
                                                size)
        # Initialize the shadow enough to be considered a
        # valid gc object.  If the original object stays
        # alive at the next minor collection, it will anyway
        # be copied over the shadow and overwrite the
        # following fields.  But if the object dies, then
        # the shadow will stay around and only be freed at
        # the next major collection, at which point we want
        # it to look valid (but ready to be freed).
        shadow = shadowhdr + size_gc_header
        self.header(shadow).tid = self.header(obj).tid
        typeid = self.get_type_id(obj)
        if self.is_varsize(typeid):
            lenofs = self.varsize_offset_to_length(typeid)
            (shadow + lenofs).signed[0] = (obj + lenofs).signed[0]
        #
        self.header(obj).tid |= GCFLAG_HAS_SHADOW
        self.nursery_objects_shadows.setitem(obj, shadow)
        return shadow

    def _find_shadow(self, obj):
        #
        # The object is not a tagged pointer, and it is still in the
        # nursery.  Find or allocate a "shadow" object, which is
        # where the object will be moved by the next minor
        # collection
        if self.header(obj).tid & GCFLAG_HAS_SHADOW:
            shadow = self.nursery_objects_shadows.get(obj)
            ll_assert(shadow != NULL,
                      "GCFLAG_HAS_SHADOW but no shadow found")
        else:
            shadow = self._allocate_shadow(obj)
        #
        # The answer is the address of the shadow.
        return shadow
    _find_shadow._dont_inline_ = True

    def id_or_identityhash(self, gcobj):
        """Implement the common logic of id() and identityhash()
        of an object, given as a GCREF.
        """
        obj = llmemory.cast_ptr_to_adr(gcobj)
        if self.is_valid_gc_object(obj):
            if self.is_in_nursery(obj):
                obj = self._find_shadow(obj)
        return llmemory.cast_adr_to_int(obj)
    id_or_identityhash._always_inline_ = True

    def id(self, gcobj):
        return self.id_or_identityhash(gcobj)

    def identityhash(self, gcobj):
        return mangle_hash(self.id_or_identityhash(gcobj))

    # ----------
    # Finalizers

    def deal_with_young_objects_with_destructors(self):
        """We can reasonably assume that destructors don't do
        anything fancy and *just* call them. Among other things
        they won't resurrect objects
        """
        while self.young_objects_with_destructors.non_empty():
            obj = self.young_objects_with_destructors.pop()
            if not self.is_forwarded(obj):
                self.call_destructor(obj)
            else:
                obj = self.get_forwarding_address(obj)
                self.old_objects_with_destructors.append(obj)

    def deal_with_old_objects_with_destructors(self):
        """We can reasonably assume that destructors don't do
        anything fancy and *just* call them. Among other things
        they won't resurrect objects
        """
        new_objects = self.AddressStack()
        while self.old_objects_with_destructors.non_empty():
            obj = self.old_objects_with_destructors.pop()
            if self.header(obj).tid & GCFLAG_VISITED:
                # surviving
                new_objects.append(obj)
            else:
                # dying
                self.call_destructor(obj)
        self.old_objects_with_destructors.delete()
        self.old_objects_with_destructors = new_objects

    def deal_with_young_objects_with_finalizers(self):
        while self.probably_young_objects_with_finalizers.non_empty():
            obj = self.probably_young_objects_with_finalizers.popleft()
            fq_nr = self.probably_young_objects_with_finalizers.popleft()
            self.singleaddr.address[0] = obj
            self._trace_drag_out1(self.singleaddr)
            obj = self.singleaddr.address[0]
            self.old_objects_with_finalizers.append(obj)
            self.old_objects_with_finalizers.append(fq_nr)

    def deal_with_objects_with_finalizers(self):
        # Walk over list of objects with finalizers.
        # If it is not surviving, add it to the list of to-be-called
        # finalizers and make it survive, to make the finalizer runnable.
        # We try to run the finalizers in a "reasonable" order, like
        # CPython does.  The details of this algorithm are in
        # pypy/doc/discussion/finalizer-order.txt.
        new_with_finalizer = self.AddressDeque()
        marked = self.AddressDeque()
        pending = self.AddressStack()
        self.tmpstack = self.AddressStack()
        while self.old_objects_with_finalizers.non_empty():
            x = self.old_objects_with_finalizers.popleft()
            fq_nr = self.old_objects_with_finalizers.popleft()
            ll_assert(self._finalization_state(x) != 1,
                      "bad finalization state 1")
            if self.header(x).tid & GCFLAG_VISITED:
                new_with_finalizer.append(x)
                new_with_finalizer.append(fq_nr)
                continue
            marked.append(x)
            marked.append(fq_nr)
            pending.append(x)
            while pending.non_empty():
                y = pending.pop()
                state = self._finalization_state(y)
                if state == 0:
                    self._bump_finalization_state_from_0_to_1(y)
                    self.trace(y, self._append_if_nonnull, pending, None)
                elif state == 2:
                    self._recursively_bump_finalization_state_from_2_to_3(y)
            self._recursively_bump_finalization_state_from_1_to_2(x)

        while marked.non_empty():
            x = marked.popleft()
            fq_nr = marked.popleft()
            state = self._finalization_state(x)
            ll_assert(state >= 2, "unexpected finalization state < 2")
            if state == 2:
                from rpython.rtyper.lltypesystem import rffi
                fq_index = rffi.cast(lltype.Signed, fq_nr)
                self.mark_finalizer_to_run(fq_index, x)
                # we must also fix the state from 2 to 3 here, otherwise
                # we leave the GCFLAG_FINALIZATION_ORDERING bit behind
                # which will confuse the next collection
                self._recursively_bump_finalization_state_from_2_to_3(x)
            else:
                new_with_finalizer.append(x)
                new_with_finalizer.append(fq_nr)

        self.tmpstack.delete()
        pending.delete()
        marked.delete()
        self.old_objects_with_finalizers.delete()
        self.old_objects_with_finalizers = new_with_finalizer

    def _append_if_nonnull(pointer, stack, ignored):
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
        ll_assert(self._finalization_state(obj) == 0,
                  "unexpected finalization state != 0")
        size_gc_header = self.gcheaderbuilder.size_gc_header
        totalsize = size_gc_header + self.get_size(obj)
        hdr = self.header(obj)
        hdr.tid |= GCFLAG_FINALIZATION_ORDERING
        self.kept_alive_by_finalizer += raw_malloc_usage(totalsize)

    def _recursively_bump_finalization_state_from_2_to_3(self, obj):
        ll_assert(self._finalization_state(obj) == 2,
                  "unexpected finalization state != 2")
        pending = self.tmpstack
        ll_assert(not pending.non_empty(), "tmpstack not empty")
        pending.append(obj)
        while pending.non_empty():
            y = pending.pop()
            hdr = self.header(y)
            if hdr.tid & GCFLAG_FINALIZATION_ORDERING:     # state 2 ?
                hdr.tid &= ~GCFLAG_FINALIZATION_ORDERING   # change to state 3
                self.trace(y, self._append_if_nonnull, pending, None)

    def _recursively_bump_finalization_state_from_1_to_2(self, obj):
        # recursively convert objects from state 1 to state 2.
        # The call to visit_all_objects() will add the GCFLAG_VISITED
        # recursively.
        self.objects_to_trace.append(obj)
        self.visit_all_objects()


    # ----------
    # Weakrefs

    # The code relies on the fact that no weakref can be an old object
    # weakly pointing to a young object.  Indeed, weakrefs are immutable
    # so they cannot point to an object that was created after it.
    # Thanks to this, during a minor collection, we don't have to fix
    # or clear the address stored in old weakrefs.
    def invalidate_young_weakrefs(self):
        """Called during a nursery collection."""
        # walk over the list of objects that contain weakrefs and are in the
        # nursery.  if the object it references survives then update the
        # weakref; otherwise invalidate the weakref
        while self.young_objects_with_weakrefs.non_empty():
            obj = self.young_objects_with_weakrefs.pop()
            if not self.is_forwarded(obj):
                continue # weakref itself dies
            obj = self.get_forwarding_address(obj)
            offset = self.weakpointer_offset(self.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            if self.is_in_nursery(pointing_to):
                if self.is_forwarded(pointing_to):
                    (obj + offset).address[0] = self.get_forwarding_address(
                        pointing_to)
                else:
                    (obj + offset).address[0] = llmemory.NULL
                    continue    # no need to remember this weakref any longer
            #
            elif (bool(self.young_rawmalloced_objects) and
                  self.young_rawmalloced_objects.contains(pointing_to)):
                # young weakref to a young raw-malloced object
                if self.header(pointing_to).tid & GCFLAG_VISITED:
                    pass    # survives, but does not move
                else:
                    (obj + offset).address[0] = llmemory.NULL
                    continue    # no need to remember this weakref any longer
            #
            elif self.header(pointing_to).tid & GCFLAG_NO_HEAP_PTRS:
                # see test_weakref_to_prebuilt: it's not useful to put
                # weakrefs into 'old_objects_with_weakrefs' if they point
                # to a prebuilt object (they are immortal).  If moreover
                # the 'pointing_to' prebuilt object still has the
                # GCFLAG_NO_HEAP_PTRS flag, then it's even wrong, because
                # 'pointing_to' will not get the GCFLAG_VISITED during
                # the next major collection.  Solve this by not registering
                # the weakref into 'old_objects_with_weakrefs'.
                continue
            #
            self.old_objects_with_weakrefs.append(obj)

    def invalidate_old_weakrefs(self):
        """Called during a major collection."""
        # walk over list of objects that contain weakrefs
        # if the object it references does not survive, invalidate the weakref
        new_with_weakref = self.AddressStack()
        while self.old_objects_with_weakrefs.non_empty():
            obj = self.old_objects_with_weakrefs.pop()
            if self.header(obj).tid & GCFLAG_VISITED == 0:
                continue # weakref itself dies
            offset = self.weakpointer_offset(self.get_type_id(obj))
            pointing_to = (obj + offset).address[0]
            ll_assert((self.header(pointing_to).tid & GCFLAG_NO_HEAP_PTRS)
                      == 0, "registered old weakref should not "
                            "point to a NO_HEAP_PTRS obj")
            if self.header(pointing_to).tid & GCFLAG_VISITED:
                new_with_weakref.append(obj)
            else:
                (obj + offset).address[0] = llmemory.NULL
        self.old_objects_with_weakrefs.delete()
        self.old_objects_with_weakrefs = new_with_weakref
