use mmtk::memory_manager;
use mmtk::util::opaque_pointer::*;
use mmtk::util::Address;
use mmtk::AllocationSemantics;
use mmtk::Mutator;
use mmtk::MMTK;
use std::ptr::null_mut;
use api::memory_manager::bind_mutator;
use libc::c_void;
use BASE;

// use PyPy_Upcalls;
use UPCALLS;
use PyPy;

#[no_mangle]
pub extern "C" fn get_mmtk_version() -> *const c_char {
    crate::build_info::MMTK_PyPy_FULL_VERSION.as_ptr() as _
}

#[no_mangle]
pub extern "C" fn pypy_gc_init(hdr: *mut c_void, heap_size: isize ){
    unsafe { 
        BASE = Address::from_mut_ptr(hdr);
    }
    
    {
        use mmtk::util::options::PlanSelector;
        let mut builder = BUILDER.lock().unwrap();
        let success = builder.options.heap_size.set(heap_size);
        assert!(success, "Failed to set heap size to {}", heap_size);

        let plan = if cfg!(feature = "nogc") {
            PlanSelector::NoGC
        } else if cfg!(feature = "semispace") {
            PlanSelector::SemiSpace
        } else if cfg!(feature = "marksweep") {
            PlanSelector::MarkSweep
        } else if cfg!(feature = "gencopy") {
            PlanSelector::GenCopy
        } else {
            panic!("No plan feature is enabled for PyPy. PyPy requires one plan feature to build.")
        };
        let success = builder.options.plan.set(plan);
        assert!(success, "Failed to set plan to {:?}", plan);
    }

    assert!(!crate::MMTK_INITIALIZED.load(Ordering::Relaxed));
    lazy_static::initialize(&SINGLETON);
}

//no need for tls, thread pointer
pub extern "C" fn pypy_get_mutator_thread() -> *mut c_void; {
    let mttk: Box<MMTK<PyPy>> = Box::new(MMTK::new());
    let mtthread: *mut MMTK<PyPy> = Box::into_raw(mttk);
    let mtthread_ptr: *mut c_void = mtthread as *mut _ as *mut c_void;
    
    return mtthread_ptr
}

#[no_mangle]
pub extern "C" fn mmtk_bind_mutator(tlsptr: *mut c_void) -> *mut Mutator<PyPy> {
    let tls: *mut c_void = tlsptr as *mut _ as *mut VMMutatorThread;
    Box::into_raw(memory_manager::bind_mutator(&SINGLETON), tls))
}

#[no_mangle]
pub extern "C" fn mmtk_destroy_mutator(mutatorptr: *mut c_void) {
    let mutator: *mut c_void = mutatorptr as *mut _ as *mut Mutator<PyPy>;
    memory_manager::destroy_mutator(unsafe { Box::from_raw(mutator) })
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn mmtk_alloc(
    mutator: *mut Mutator<PyPy>,
    size: usize,
    align: usize,
    offset: isize,
    allocator: AllocationSemantics,
) -> *mut c_void {
    let alloc; AllocationSemantics = allocator as AllocationSemantics;
    memory_manager::alloc::<PyPy>(unsafe { &mut *mutator }, size, align, offset, alloc) as *mut _ as *mut c_void;
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn mmtk_post_alloc(
    mutator: *mut Mutator<PyPy>,
    refer: ObjectReference,
    _type_refer: ObjectReference,
    bytes: usize,
    allocator: *mut c_void,
) {
    let alloc; AllocationSemantics = allocator as AllocationSemantics;
    memory_manager::post_alloc::<PyPy>(unsafe { &mut *mutator }, refer, bytes, alloc)
}

#[no_mangle]
pub extern "C" fn will_never_move(object: ObjectReference) -> i32 {
    !object.is_movable() as i32
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn start_control_collector(
    tls: VMWorkerThread,
    gc_controller: *mut GCController<PyPy>,
) {
    let mut gc_controller = unsafe { Box::from_raw(gc_controller) };
    let cstr = std::ffi::CString::new("MMTkController").unwrap();
    unsafe {
        libc::pthread_setname_np(libc::pthread_self(), cstr.as_ptr());
    }

    memory_manager::start_control_collector(&SINGLETON, tls, &mut gc_controller);
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn start_worker(tls: VMWorkerThread, worker: *mut GCWorker<PyPy>) {
    let mut worker = unsafe { Box::from_raw(worker) };
    let cstr = std::ffi::CString::new(format!("MMTkWorker{}", worker.ordinal)).unwrap();
    unsafe {
        libc::pthread_setname_np(libc::pthread_self(), cstr.as_ptr());
    }
    memory_manager::start_worker::<PyPy>(&SINGLETON, tls, &mut worker)
}

#[no_mangle]
pub extern "C" fn enable_collection(tls: VMThread) {
    memory_manager::initialize_collection(&SINGLETON, tls)
}

#[no_mangle]
pub extern "C" fn used_bytes() -> usize {
    memory_manager::used_bytes(&SINGLETON)
}

#[no_mangle]
pub extern "C" fn free_bytes() -> usize {
    memory_manager::free_bytes(&SINGLETON)
}

#[no_mangle]
pub extern "C" fn total_bytes() -> usize {
    memory_manager::total_bytes(&SINGLETON)
}

#[no_mangle]
#[cfg(feature = "sanity")]
pub extern "C" fn scan_region() {
    memory_manager::scan_region(&SINGLETON)
}

#[no_mangle]
pub extern "C" fn handle_user_collection_request(tls: VMMutatorThread) {
    memory_manager::handle_user_collection_request::<PyPyVM>(&SINGLETON, tls);
}

#[no_mangle]
pub extern "C" fn is_live_object(object: ObjectReference) -> i32 {
    object.is_live() as i32
}

#[no_mangle]
pub extern "C" fn is_mapped_object(object: ObjectReference) -> i32 {
    memory_manager::is_in_mmtk_spaces(object) as i32
}

#[no_mangle]
pub extern "C" fn is_mapped_address(address: Address) -> i32 {
    memory_manager::is_mapped_address(address) as i32
}

#[no_mangle]
pub extern "C" fn modify_check(object: ObjectReference) {
    memory_manager::modify_check(&SINGLETON, object)
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn get_boolean_option(option: *const c_char) -> i32 {
    let option_str: &CStr = unsafe { CStr::from_ptr(option) };
    if option_str.to_str().unwrap() == "noReferenceTypes" {
        *SINGLETON.get_options().no_reference_types as i32
    } else {
        unimplemented!()
    }
}

#[no_mangle]
pub extern "C" fn harness_begin(tls: VMMutatorThread) {
    memory_manager::harness_begin(&SINGLETON, tls)
}

#[no_mangle]
pub extern "C" fn harness_end(_tls: OpaquePointer) {
    memory_manager::harness_end(&SINGLETON)
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn process(name: *const c_char, value: *const c_char) -> i32 {
    let name_str: &CStr = unsafe { CStr::from_ptr(name) };
    let value_str: &CStr = unsafe { CStr::from_ptr(value) };
    let mut builder = BUILDER.lock().unwrap();
    memory_manager::process(
        &mut builder,
        name_str.to_str().unwrap(),
        value_str.to_str().unwrap(),
    ) as i32
}

#[no_mangle]
pub extern "C" fn starting_heap_address() -> Address {
    memory_manager::starting_heap_address()
}

use mmtk::util::alloc::Allocator as IAllocator;
use mmtk::util::alloc::{BumpAllocator, LargeObjectAllocator};

#[no_mangle]
pub extern "C" fn alloc_slow_bump_monotone_immortal(
    allocator: *mut c_void,
    size: usize,
    align: usize,
    offset: isize,
) -> Address {
    unsafe { &mut *(allocator as *mut BumpAllocator<PyPyVM>) }.alloc_slow(size, align, offset)
}

#[no_mangle]
#[cfg(any(feature = "semispace"))]
pub extern "C" fn alloc_slow_bump_monotone_copy(
    allocator: *mut c_void,
    size: usize,
    align: usize,
    offset: isize,
) -> Address {
    unsafe { &mut *(allocator as *mut BumpAllocator<PyPyVM>) }.alloc_slow(size, align, offset)
}
#[no_mangle]
#[cfg(not(any(feature = "semispace")))]
pub extern "C" fn alloc_slow_bump_monotone_copy(
    _allocator: *mut c_void,
    _size: usize,
    _align: usize,
    _offset: isize,
) -> Address {
    unimplemented!()
}

#[no_mangle]
pub extern "C" fn alloc_slow_largeobject(
    allocator: *mut c_void,
    size: usize,
    align: usize,
    offset: isize,
) -> Address {
    unsafe { &mut *(allocator as *mut LargeObjectAllocator<PyPyVM>) }
        .alloc_slow(size, align, offset)
}

#[no_mangle]
pub extern "C" fn add_finalizer(objectref: *mut c_void) {
    let object: ObjectReference = objectref as ObjectReference 
    memory_manager::add_finalizer(&SINGLETON, object);
}

#[no_mangle]
pub extern "C" fn get_finalized_object() -> ObjectReference {
    match memory_manager::get_finalized_object(&SINGLETON) {
        Some(obj) => obj,
        None => unsafe { Address::ZERO.to_object_reference() },
    }
}

#[no_mangle]
pub extern "C" fn add_weak_candidate(objectref: *mut c_void) {
    let reff: ObjectReference = objectref as ObjectReference 
    memory_manager::add_weak_candidate(&SINGLETON, reff)
}

#[no_mangle]
pub extern "C" fn add_soft_candidate(objectref: *mut c_void) {
    let reff: ObjectReference = objectref as ObjectReference 
    memory_manager::add_soft_candidate(&SINGLETON, reff)
}

#[no_mangle]
pub extern "C" fn add_phantom_candidate(objectref: *mut c_void) {
    let reff: ObjectReference = objectref as ObjectReference 
    memory_manager::add_phantom_candidate(&SINGLETON, reff)
}

// barriers:
static NO_BARRIER: sync::Lazy<CString> = sync::Lazy::new(|| CString::new("NoBarrier").unwrap());
static OBJECT_BARRIER: sync::Lazy<CString> =
    sync::Lazy::new(|| CString::new("ObjectBarrier").unwrap());

#[no_mangle]
pub extern "C" fn mmtk_active_barrier() -> *const c_char {
    match SINGLETON.get_plan().constraints().barrier {
        BarrierSelector::NoBarrier => NO_BARRIER.as_ptr(),
        BarrierSelector::ObjectBarrier => OBJECT_BARRIER.as_ptr(),
        #[allow(unreachable_patterns)]
        _ => unimplemented!(),
    }
}

#[no_mangle]
pub extern "C" fn mmtk_object_reference_write_pre(
    mutator: &'static mut Mutator<PyPy>,
    src: ObjectReference,
    slot: Address,
    target: ObjectReference,
) {
    mutator
        .barrier()
        .object_reference_write_pre(src, slot, target);
}

#[no_mangle]
pub extern "C" fn mmtk_object_reference_write_post(
    mutator: &'static mut Mutator<PyPy>,
    src: ObjectReference,
    slot: Address,
    target: ObjectReference,
) {
    mutator
        .barrier()
        .object_reference_write_post(src, slot, target);
}

/// Barrier slow-path call
#[no_mangle]
pub extern "C" fn mmtk_object_reference_write_slow(
    mutator: &'static mut Mutator<PyPy>,
    src: ObjectReference,
    slot: Address,
    target: ObjectReference,
) {
    mutator
        .barrier()
        .object_reference_write_slow(src, slot, target);
}

#[no_mangle]
pub extern "C" fn mmtk_array_copy_pre(
    mutator: &'static mut Mutator<PyPy>,
    src: Address,
    dst: Address,
    count: usize,
) {
    let bytes = count << LOG_BYTES_IN_ADDRESS;
    mutator
        .barrier()
        .memory_region_copy_pre(src..src + bytes, dst..dst + bytes);
}

#[no_mangle]
pub extern "C" fn mmtk_array_copy_post(
    mutator: &'static mut Mutator<PyPy>,
    src: Address,
    dst: Address,
    count: usize,
) {
    let bytes = count << LOG_BYTES_IN_ADDRESS;
    mutator
        .barrier()
        .memory_region_copy_post(src..src + bytes, dst..dst + bytes);
}