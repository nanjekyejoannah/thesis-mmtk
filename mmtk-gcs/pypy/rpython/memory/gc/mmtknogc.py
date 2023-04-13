from rpython.rtyper.lltypesystem.llmemory import raw_malloc, raw_free
from rpython.rtyper.lltypesystem.llmemory import raw_memcopy, raw_memclear
from rpython.rtyper.lltypesystem.llmemory import NULL, raw_malloc_usage
from rpython.memory.support import get_address_stack
from rpython.memory.gcheader import GCHeaderBuilder
from rpython.rtyper.lltypesystem import lltype, llmemory, rffi, llgroup
from rpython.rlib.objectmodel import free_non_gc_object
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rlib.rarithmetic import ovfcheck
from rpython.rlib.debug import debug_print, debug_start, debug_stop
from rpython.memory.gc.base import GCBase
from rpython.rtyper.tool.rffi_platform import llexternal_mmtk


import sys, os

#init
ll_mmtk_gc_init = llexternal_mmtk('pypy_gc_init', [], lltype.Void)
ll_mmtk_is_gc_initialized = llexternal_mmtk('pypy_is_gc_initialized', [], lltype.Bool)
ll_mmtk_mmtk_set_heap_size = llexternal_mmtk('mmtk_set_heap_size', [rffi.SIGNED, rffi.SIGNED], lltype.Bool)

#mutator
ll_mmtk_bind_mutator = llexternal_mmtk('bind_mutator', [], rffi.VOIDP)

#allocation
ll_mmtk_alloc = llexternal_mmtk('mmtk_alloc', [rffi.VOIDP, rffi.SIGNED, rffi.SIGNED, rffi.SIGNED], lltype.Unsigned)

class MMTKNoGC(GCBase):
    HDR = lltype.ForwardReference()
    HDRPTR = lltype.Ptr(HDR)
    # need to maintain a linked list of malloced objects, since we used the
    # systems allocator and can't walk the heap
    HDR.become(lltype.Struct('header', ('typeid16', llgroup.HALFWORD),
                                       ('mark', lltype.Bool),
                                       ('flags', lltype.Char),
                                       ('next', HDRPTR)))
    typeid_is_in_field = 'typeid16'
    # withhash_flag_is_in_field = 'flags', FL_WITHHASH

    POOL = lltype.GcStruct('gc_pool')
    POOLPTR = lltype.Ptr(POOL)

    POOLNODE = lltype.ForwardReference()
    POOLNODEPTR = lltype.Ptr(POOLNODE)
    POOLNODE.become(lltype.Struct('gc_pool_node', ('linkedlist', HDRPTR),
                                                  ('nextnode', POOLNODEPTR)))

    TRANSLATION_PARAMS = {'start_heap_size': 8*1024*1024}

    def __init__(self, config, start_heap_size=4096, **kwds):
        self.param_start_heap_size = start_heap_size
        self.space_size = 4096
        GCBase.__init__(self, config, **kwds)
        ll_mmtk_gc_init()
        ll_mmtk_is_gc_initialized()
        ll_mmtk_mmtk_set_heap_size(self.space_size, self.param_start_heap_size)


    def setup(self):
        GCBase.setup(self)
        self.heap_usage = 0   
        self.bytes_malloced = 0
        self.bytes_malloced_threshold = self.param_start_heap_size
        self.total_collection_time = 0.0
        self.malloced_objects = lltype.nullptr(self.HDR)
        self.malloced_objects_with_finalizer = lltype.nullptr(self.HDR)
        self.objects_with_finalizers = self.AddressDeque()
        self.objects_with_light_finalizers = self.AddressStack()
        self.objects_with_weakrefs = self.AddressStack()
        self.objects_with_weak_pointers = lltype.nullptr(self.HDR)
        self.curpool = lltype.nullptr(self.POOL)
        self.poolnodes = lltype.nullptr(self.POOLNODE)
        self.collect_in_progress = False
        self.prev_collect_end_time = 0.0

    def register_finalizer(self, fq_index, gcobj):
        pass

    def maybe_collect(self):
        pass

    def write_malloc_statistics(self, typeid16, size, result, varsize):
        pass

    def write_free_statistics(self, typeid16, result):
        pass

    def malloc_fixedsize(self, typeid16, size,
                         has_finalizer=False, is_finalizer_light=False,
                         contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        total_size = size_gc_header + size
        try:
            tot_size = size_gc_header + size
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        handle = ll_mmtk_bind_mutator()
        addr_ptr = ll_mmtk_alloc(handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        hdr = result
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        if has_finalizer:
            hdr.next = self.malloced_objects_with_finalizer
            self.malloced_objects_with_finalizer = hdr
        elif contains_weakptr:
            hdr.next = self.objects_with_weak_pointers
            self.objects_with_weak_pointers = hdr
        else:
            hdr.next = self.malloced_objects
            self.malloced_objects = hdr
        self.bytes_malloced = bytes_malloced
        result += size_gc_header
        self.write_malloc_statistics(typeid16, tot_size, result, False)
        ll_mmtk_post_alloc(self.handle, addr_ptr, self.bytes_malloced)
        return llmemory.cast_adr_to_ptr(result, llmemory.GCREF)
    malloc_fixedsize._dont_inline_ = True

    def malloc_fixedsize_clear(self, typeid16, size,
                               has_finalizer=False,
                               is_finalizer_light=False,
                               contains_weakptr=False):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        total_size = size_gc_header + size
        try:
            tot_size = size_gc_header + size
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        addr_ptr = ll_mmtk_alloc(self.handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        raw_memclear(result, tot_size)
        hdr = result
        hdr.typeid16 = typeid16
        hdr.mark = False
        hdr.flags = '\x00'
        if has_finalizer:
            hdr.next = self.malloced_objects_with_finalizer
            self.malloced_objects_with_finalizer = hdr
        elif contains_weakptr:
            hdr.next = self.objects_with_weak_pointers
            self.objects_with_weak_pointers = hdr
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
                       offset_to_length):
        self.maybe_collect()
        size_gc_header = self.gcheaderbuilder.size_gc_header
        total_size = size_gc_header + size
        try:
            fixsize = size_gc_header + size
            varsize = ovfcheck(itemsize * length)
            tot_size = ovfcheck(fixsize + varsize)
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        handle = ll_mmtk_bind_mutator()
        addr_ptr = ll_mmtk_alloc(handle, total_size, 8, size_gc_header)
        result = cast_adr_to_int(llmemory.cast_ptr_to_adr(addr_ptr))
        if not result:
            raise memoryError
        (result + size_gc_header + offset_to_length).signed[0] = length
        hdr = result
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
                             offset_to_length):
        size_gc_header = self.gcheaderbuilder.size_gc_header
        total_size = size_gc_header + size
        try:
            fixsize = size_gc_header + size
            varsize = ovfcheck(itemsize * length)
            tot_size = ovfcheck(fixsize + varsize)
            usage = raw_malloc_usage(tot_size)
            bytes_malloced = ovfcheck(self.bytes_malloced+usage)
            ovfcheck(self.heap_usage + bytes_malloced)
        except OverflowError:
            raise memoryError
        handle = ll_mmtk_bind_mutator()
        result = llmemory.cast_ptr_to_adr(ll_mmtk_alloc(handle, total_size, 8, size_gc_header))
        if not result:
            raise memoryError
        raw_memclear(result, tot_size)        
        (result + size_gc_header + offset_to_length).signed[0] = length
        hdr = result
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
        raise NotImplementedError("old operation deprecated")

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
        hdr = llmemory.cast_adr_to_ptr(addr, self.HDRPTR)
        hdr.typeid16 = typeid
        hdr.mark = True
        hdr.flags = '\x00'

    # experimental support for thread cloning
    def x_swap_pool(self, newpool):
        raise NotImplementedError("old operation deprecated")

    def x_clone(self, clonedata):
        raise NotImplementedError("old operation deprecated")

    def identityhash(self, obj):
        obj = llmemory.cast_ptr_to_adr(obj)
        hdr = self.header(obj)
        if ord(hdr.flags) & FL_WITHHASH:
            obj += self.get_size(obj)
            return obj.signed[0]
        else:
            return llmemory.cast_adr_to_int(obj)


class PrintingMmtkNoGC(MMTKNoGC):
    pass
