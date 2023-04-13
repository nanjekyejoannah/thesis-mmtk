#include <cstdarg>
#include <cstdint>
#include <cstdlib>
#include <ostream>
#include <new>

struct OpenJDK;

extern "C" {

extern const uintptr_t GLOBAL_SIDE_METADATA_BASE_ADDRESS;

extern const uintptr_t GLOBAL_SIDE_METADATA_VM_BASE_ADDRESS;

extern const uintptr_t GLOBAL_ALLOC_BIT_ADDRESS;

extern const uintptr_t FREE_LIST_ALLOCATOR_SIZE;

extern const uintptr_t MMTK_MARK_COMPACT_HEADER_RESERVED_IN_BYTES;

const char *get_mmtk_version();

const char *mmtk_active_barrier();

/// # Safety
/// Caller needs to make sure the ptr is a valid vector pointer.
void release_buffer(Address *ptr, uintptr_t length, uintptr_t capacity);

void pypy_gc_init();

bool pypy_is_gc_initialized();

bool mmtk_set_heap_size(uintptr_t min, uintptr_t max);

void *bind_mutator();

void destroy_mutator(void *mutatorptr);

void flush_mutator(void *mutatorptr);

Address alloc(Mutator<OpenJDK> *mutator,
              uintptr_t size,
              uintptr_t align,
              intptr_t offset,
              AllocationSemantics allocator);

AllocatorSelector get_allocator_mapping(AllocationSemantics allocator);

uintptr_t get_max_non_los_default_alloc_bytes();

void post_alloc(Mutator<OpenJDK> *mutator,
                ObjectReference refer,
                uintptr_t bytes,
                AllocationSemantics allocator);

bool will_never_move(ObjectReference object);

void start_control_collector(VMWorkerThread tls, GCController<OpenJDK> *gc_controller);

void start_worker(VMWorkerThread tls, GCWorker<OpenJDK> *worker);

void initialize_collection(VMThread tls);

uintptr_t used_bytes();

uintptr_t free_bytes();

uintptr_t total_bytes();

void scan_region();

void handle_user_collection_request(VMMutatorThread tls);

bool is_in_mmtk_spaces(ObjectReference object);

bool is_mapped_address(Address addr);

void modify_check(ObjectReference object);

void add_weak_candidate(ObjectReference reff);

void add_soft_candidate(ObjectReference reff);

void add_phantom_candidate(ObjectReference reff);

void harness_begin(uintptr_t _id);

void mmtk_harness_begin_impl();

void harness_end(uintptr_t _id);

void mmtk_harness_end_impl();

bool process(const char *name, const char *value);

bool process_bulk(const char *options);

Address starting_heap_address();

Address last_heap_address();

uintptr_t openjdk_max_capacity();

bool executable();

/// Full pre barrier
void mmtk_object_reference_write_pre(Mutator<OpenJDK> *mutator,
                                     ObjectReference src,
                                     Address slot,
                                     ObjectReference target);

/// Full post barrier
void mmtk_object_reference_write_post(Mutator<OpenJDK> *mutator,
                                      ObjectReference src,
                                      Address slot,
                                      ObjectReference target);

/// Barrier slow-path call
void mmtk_object_reference_write_slow(Mutator<OpenJDK> *mutator,
                                      ObjectReference src,
                                      Address slot,
                                      ObjectReference target);

/// Array-copy pre-barrier
void mmtk_array_copy_pre(Mutator<OpenJDK> *mutator, Address src, Address dst, uintptr_t count);

/// Array-copy post-barrier
void mmtk_array_copy_post(Mutator<OpenJDK> *mutator, Address src, Address dst, uintptr_t count);

/// C2 Slowpath allocation barrier
void mmtk_object_probable_write(Mutator<OpenJDK> *mutator, ObjectReference obj);

void add_finalizer(ObjectReference object);

ObjectReference get_finalized_object();

/// Report a list of pointers in nmethod to mmtk.
void mmtk_add_nmethod_oop(Address addr);

/// Register a nmethod.
/// The c++ part of the binding should scan the nmethod and report all the pointers to mmtk first, before calling this function.
/// This function will transfer all the locally cached pointers of this nmethod to the global storage.
void mmtk_register_nmethod(Address nm);

/// Unregister a nmethod.
void mmtk_unregister_nmethod(Address nm);

} // extern "C"
