from pypy.rpython.lltypesystem.llmemory import raw_malloc, raw_free
from pypy.rpython.lltypesystem.llmemory import raw_memcopy, raw_memclear
from pypy.rpython.lltypesystem.llmemory import NULL, raw_malloc_usage
from pypy.rpython.memory.support import DEFAULT_CHUNK_SIZE
from pypy.rpython.memory.support import get_address_stack
from pypy.rpython.memory.gcheader import GCHeaderBuilder
from pypy.rpython.lltypesystem import lltype, llmemory, rffi, llgroup
from pypy.rlib.objectmodel import free_non_gc_object
from pypy.rpython.lltypesystem.lloperation import llop
from pypy.rlib.rarithmetic import ovfcheck
from pypy.rlib.debug import debug_print, debug_start, debug_stop
from pypy.rpython.memory.gc.base import GCBase
from rpython.rtyper.tool.rffi_platform import llexternal_mmtk


import sys, os

X_POOL = lltype.GcOpaqueType('gc.pool')
X_POOL_PTR = lltype.Ptr(X_POOL)
X_CLONE = lltype.GcStruct('CloneData', ('gcobjectptr', llmemory.GCREF),
                                       ('pool',        X_POOL_PTR))
X_CLONE_PTR = lltype.Ptr(X_CLONE)

FL_WITHHASH = 0x01
FL_CURPOOL  = 0x02

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


memoryError = MemoryError()

class MarkSweepGC(GCBase):
    HDR = lltype.ForwardReference()
    HDRPTR = lltype.Ptr(HDR)
    # need to maintain a linked list of malloced objects, since we used the
    # systems allocator and can't walk the heap
    HDR.become(lltype.Struct('header', ('typeid16', llgroup.HALFWORD),
                                       ('mark', lltype.Bool),
                                       ('flags', lltype.Char),
                                       ('next', HDRPTR)))
    typeid_is_in_field = 'typeid16'
    withhash_flag_is_in_field = 'flags', FL_WITHHASH

    POOL = lltype.GcStruct('gc_pool')
    POOLPTR = lltype.Ptr(POOL)

    POOLNODE = lltype.ForwardReference()
    POOLNODEPTR = lltype.Ptr(POOLNODE)
    POOLNODE.become(lltype.Struct('gc_pool_node', ('linkedlist', HDRPTR),
                                                  ('nextnode', POOLNODEPTR)))

    # the following values override the default arguments of __init__ when
    # translating to a real backend.
    TRANSLATION_PARAMS = {'start_heap_size': 8*1024*1024} # XXX adjust

    def __init__(self, config, chunk_size=DEFAULT_CHUNK_SIZE, start_heap_size=4096):
        self.param_start_heap_size = start_heap_size
        GCBase.__init__(self, config, chunk_size)
        ll_mmtk_gc_init() 
        ll_assert(ll_mmtk_is_gc_initialized() == True, 
        "MMTK initialized successfully")
        ll_assert(ll_mmtk_mmtk_set_heap_size(self.param_space_size, self.param_max_space_size)  == True,
        "MMTK heap size set successfully")
        self.handle = ll_mmtk_bind_mutator()


    def setup(self):
        GCBase.setup(self)
        self.heap_usage = 0          # at the end of the latest collection
        self.bytes_malloced = 0      # since the latest collection
        self.bytes_malloced_threshold = self.param_start_heap_size
        self.total_collection_time = 0.0
        self.malloced_objects = lltype.nullptr(self.HDR)
        self.malloced_objects_with_finalizer = lltype.nullptr(self.HDR)
        self.objects_with_finalizers = self.AddressDeque()
        self.objects_with_light_finalizers = self.AddressStack()
        self.objects_with_weakrefs = self.AddressStack()

        self.objects_with_finalizers = self.AddressDeque()
        self.objects_with_light_finalizers = self.AddressStack()
        self.objects_with_weakrefs = self.AddressStack()

    def _teardown(self):
        debug_print("Teardown")
        ll_mmtk_destroy_mutator(self.mmtk_handle)
        ll_mmtk_flush_mutator(self.mmtk_handle)
        

    def maybe_collect(self):
        return NotImplemented

    def write_malloc_statistics(self, typeid16, size, result, varsize):
        return NotImplemented

    def write_free_statistics(self, typeid16, result):
        return NotImplemented

    def malloc_fixedsize(self, typeid16, size, can_collect,
                         has_finalizer=False, contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        try:
            tot_size = size_gc_header + size
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        #result = raw_malloc(tot_size)
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        hdr = llmemory.cast_adr_to_ptr(result, self.HDRPTR)
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        if has_finalizer:
            hdr.next = self.malloced_objects_with_finalizer
            self.malloced_objects_with_finalizer = hdr
            ll_mmtk_add_finalizer(llmemory.cast_adr_to_ptr(hdr, llmemory.GCREF))
        elif contains_weakptr:
            hdr.next = self.objects_with_weak_pointers
            self.objects_with_weak_pointers = hdr
            ll_mmtk_add_weak_candidate(llmemory.cast_adr_to_ptr(hdr, llmemory.GCREF))
        else:
            hdr.next = self.malloced_objects
            self.malloced_objects = hdr
        self.bytes_malloced = bytes_malloced
        result += size_gc_header
        self.write_malloc_statistics(typeid16, tot_size, result, False)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result, llmemory.GCREF)
    malloc_fixedsize._dont_inline_ = True

    def malloc_fixedsize_clear(self, typeid16, size, can_collect,
                               has_finalizer=False, contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        try:
            fixsize = size_gc_header + size
            varsize = ovfcheck(itemsize * length)
            tot_size = ovfcheck(fixsize + varsize)
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        raw_memclear(result, tot_size)
        hdr = llmemory.cast_adr_to_ptr(result, self.HDRPTR)
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        if has_finalizer:
            hdr.next = self.malloced_objects_with_finalizer
            self.malloced_objects_with_finalizer = hdr
            ll_mmtk_add_finalizer(llmemory.cast_adr_to_ptr(hdr, llmemory.GCREF))
        elif contains_weakptr:
            hdr.next = self.objects_with_weak_pointers
            self.objects_with_weak_pointers = hdr
            ll_mmtk_add_weak_candidate(llmemory.cast_adr_to_ptr(hdr, llmemory.GCREF))
        else:
            hdr.next = self.malloced_objects
            self.malloced_objects = hdr
        self.bytes_malloced = bytes_malloced
        result += size_gc_header
        self.write_malloc_statistics(typeid16, tot_size, result, False)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result, llmemory.GCREF)
    malloc_fixedsize_clear._dont_inline_ = True

    def malloc_varsize(self, typeid16, length, size, itemsize,
                       offset_to_length, can_collect):
        if can_collect:
            self.maybe_collect()
        size_gc_header = self.gcheaderbuilder.size_gc_header
        try:
            fixsize = size_gc_header + size
            varsize = ovfcheck(itemsize * length)
            tot_size = ovfcheck(fixsize + varsize)
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        (result + size_gc_header + offset_to_length).signed[0] = length
        hdr = llmemory.cast_adr_to_ptr(result, self.HDRPTR)
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        hdr.next = self.malloced_objects
        self.malloced_objects = hdr
        self.bytes_malloced = bytes_malloced
            
        result += size_gc_header
        self.write_malloc_statistics(typeid16, tot_size, result, True)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result, llmemory.GCREF)
    malloc_varsize._dont_inline_ = True

    def malloc_varsize_clear(self, typeid16, length, size, itemsize,
                             offset_to_length, can_collect):
        if can_collect:
            self.maybe_collect()
        size_gc_header = self.gcheaderbuilder.size_gc_header
        try:
            fixsize = size_gc_header + size
            varsize = ovfcheck(itemsize * length)
            tot_size = ovfcheck(fixsize + varsize)
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        addr_ptr = ll_mmtk_alloc(self.mmtk_handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        raw_memclear(result, tot_size)        
        (result + size_gc_header + offset_to_length).signed[0] = length
        hdr = llmemory.cast_adr_to_ptr(result, self.HDRPTR)
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        hdr.next = self.malloced_objects
        self.malloced_objects = hdr
        self.bytes_malloced = bytes_malloced
            
        result += size_gc_header
        self.write_malloc_statistics(typeid16, tot_size, result, True)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result, llmemory.GCREF)
    malloc_varsize_clear._dont_inline_ = True

    def collect(self, gen=0):
        return NotImplemented
        

    def _mark_root(self, root):
        gcobjectaddr = root.address[0]
        self._mark_stack.append(gcobjectaddr)

    def _mark_root_and_clear_bit(self, root):
        gcobjectaddr = root.address[0]
        self._mark_stack.append(gcobjectaddr)
        size_gc_header = self.gcheaderbuilder.size_gc_header
        gc_info = gcobjectaddr - size_gc_header
        hdr = llmemory.cast_adr_to_ptr(gc_info, self.HDRPTR)
        hdr.mark = False

    STAT_HEAP_USAGE     = 0
    STAT_BYTES_MALLOCED = 1
    STATISTICS_NUMBERS  = 2

    def get_type_id(self, obj):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        gc_info = obj - size_gc_header
        hdr = llmemory.cast_adr_to_ptr(gc_info, self.HDRPTR)
        return hdr.typeid16

    def add_reachable_to_stack(self, obj, objects):
        self.trace(obj, self._add_reachable, objects)

    def _add_reachable(pointer, objects):
        obj = pointer.address[0]
        objects.append(obj)
    _add_reachable = staticmethod(_add_reachable)

    def statistics(self, index):
        # no memory allocation here!
        if index == self.STAT_HEAP_USAGE:
            return self.heap_usage
        if index == self.STAT_BYTES_MALLOCED:
            return self.bytes_malloced
        return -1

    def init_gc_object(self, addr, typeid):
        hdr = llmemory.cast_adr_to_ptr(addr, self.HDRPTR)
        hdr.typeid16 = typeid
        hdr.mark = False
        hdr.flags = '\x00'

    def init_gc_object_immortal(self, addr, typeid, flags=0):
        # prebuilt gc structures always have the mark bit set
        # ignore flags
        hdr = llmemory.cast_adr_to_ptr(addr, self.HDRPTR)
        hdr.typeid16 = typeid
        hdr.mark = True
        hdr.flags = '\x00'

    def x_swap_pool(self, newpool):
        return NotImplemented

    def x_clone(self, clonedata):
        return NotImplemented

    def identityhash(self, obj):
        obj = llmemory.cast_ptr_to_adr(obj)
        hdr = self.header(obj)
        if ord(hdr.flags) & FL_WITHHASH:
            obj += self.get_size(obj)
            return obj.signed[0]
        else:
            return llmemory.cast_adr_to_int(obj)


class PrintingMarkSweepGC(MarkSweepGC):
    _alloc_flavor_ = "raw"
    COLLECT_EVERY = 2000

    def __init__(self, chunk_size=DEFAULT_CHUNK_SIZE, start_heap_size=4096):
        MarkSweepGC.__init__(self, chunk_size, start_heap_size)
        self.count_mallocs = 0

    def maybe_collect(self):
        return NotImplemented

    def write_malloc_statistics(self, typeid, size, result, varsize):
        if varsize:
            what = "malloc_varsize"
        else:
            what = "malloc"
        llop.debug_print(lltype.Void, what, typeid, " ", size, " ", result)

    def write_free_statistics(self, typeid, result):
        llop.debug_print(lltype.Void, "free", typeid, " ", result)

    def collect(self, gen=0):
        return NotImplemented
