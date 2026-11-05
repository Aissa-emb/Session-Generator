"""Main-thread hang stacks for the Apple SDK (embrace-apple-sdk 6.21.0, HangCaptureService).

- FrameRateMonitor reports a hang after 249 ms (HangLimits.defaultHangThreshold). The span is
  ``emb-thread-blockage`` (``perf.thread_blockage``) with ``last_known_time_unix_nano`` (hang start),
  ``interval_code`` "0" and ``thread_priority`` "0".
- StallTriggeredSampler starts sampling once the main thread has stalled for 150 ms (polling every 50 ms). When
  the hang ends, the first sample inside it becomes one ``thread_blockage_sample`` event, timestamped at the hang
  start, with ``emb.type`` ``perf.thread_blockage_sample``, ``sample_overhead`` (ns), ``frame_count`` and
  ``emb.stacktrace.ios``: the base64 of a JSON array of frames, innermost first.
- Frames are symbolicated and demangled on device (KSCrash demangle filter); each is
  {"a": "0x%016llx", "m": image, "o": address - image base, "p": image, "s": symbol, "so": symbol - image base,
  "u": image UUID}. Frames without an image or symbol are dropped.
"""
import base64
import hashlib
import json

STORIES = {
    "worker_lock": ("result_store_wait", "remote_config_value_read", "token_store_contention"),
    "migration": ("cache_migration", "defaults_large_write", "cached_feed_decode"),
    "layout": ("cell_date_formatting", "image_decode_in_cell"),
}
DOMAIN = {"ecommerce": ("Catalog", "ProductFeed", "ProductCell", "BagState"),
          "banking": ("Accounts", "TransactionFeed", "TransactionCell", "TransferDraft"),
          "travel": ("Trips", "FareFeed", "ItineraryCell", "BookingDraft")}


def _h(*parts):
    return int(hashlib.md5(":".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()[:12], 16)


class Images:
    """Load addresses: the app binary slides per launch, system libraries share the dyld cache slide per boot."""

    def __init__(self, os_version, app_name, app_uuid, process_id, device_id):
        self.os, self.app, self.app_uuid = os_version, app_name, app_uuid
        self.app_base = 0x100000000 + (_h(process_id) % 0x8000) * 0x4000
        self.cache_slide = (_h(device_id, os_version) % 0x2000) * 0x10000

    def uuid(self, image):
        if image == self.app:
            u = self.app_uuid
        else:
            u = hashlib.md5(f"{image}:{self.os}".encode(), usedforsecurity=False).hexdigest().upper()
        return f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:32]}"

    def base(self, image):
        if image == self.app:
            return self.app_base
        return 0x180000000 + self.cache_slide + (_h(image, self.os) % 0x600) * 0x100000

    def frame(self, image, symbol):
        sym_off = 0x1000 + (_h(image, symbol, self.os) % (0x3fffff if image == self.app else 0x7fffff)) * 4
        within = 4 * (4 + _h(symbol, "pc") % 180)
        base = self.base(image)
        return {"a": f"0x{base + sym_off + within:016x}", "m": image, "o": sym_off + within, "p": image,
                "s": symbol, "so": sym_off, "u": self.uuid(image)}


def _run_loop(img, source):
    root = [("dyld", "start"), (img.app, "main"), ("UIKitCore", "UIApplicationMain"),
            ("UIKitCore", "-[UIApplication _run]"), ("GraphicsServices", "GSEventRunModal"),
            ("CoreFoundation", "CFRunLoopRunSpecific"), ("CoreFoundation", "__CFRunLoopRun")]
    if source == "main_queue":
        return root + [("CoreFoundation", "__CFRUNLOOP_IS_SERVICING_THE_MAIN_DISPATCH_QUEUE__"),
                       ("libdispatch.dylib", "_dispatch_main_queue_drain"),
                       ("libdispatch.dylib", "_dispatch_client_callout"),
                       ("libswift_Concurrency.dylib", "swift::runJobInEstablishedExecutorContext(swift::Job*)")]
    if source == "layout":
        return root + [("CoreFoundation", "__CFRunLoopDoObservers"),
                       ("CoreFoundation", "__CFRUNLOOP_IS_CALLING_OUT_TO_AN_OBSERVER_CALLBACK_FUNCTION__"),
                       ("UIKitCore", "_UIUpdateSequenceRun"), ("QuartzCore", "CA::Transaction::commit()"),
                       ("QuartzCore", "CA::Layer::layout_and_display_if_needed(CA::Transaction*)"),
                       ("QuartzCore", "CA::Layer::layout_if_needed(CA::Transaction*)"),
                       ("UIKitCore", "-[UIView(CALayerDelegate) layoutSublayersOfLayer:]"),
                       ("UIKitCore", "-[UICollectionView layoutSubviews]"),
                       ("UIKitCore", "-[UICollectionView _updateVisibleCellsNow:]"),
                       ("UIKitCore", "-[UICollectionView _createPreparedCellForItemAtIndexPath:withLayoutAttributes:applyAttributes:isFocused:notify:]"),
                       ("UIKitCore", "-[UICollectionViewDiffableDataSource collectionView:cellForItemAtIndexPath:]")]
    # A tap: the touch is delivered from the event queue source and the control sends its action.
    return root + [("CoreFoundation", "__CFRunLoopDoSource0"),
                   ("CoreFoundation", "__CFRUNLOOP_IS_CALLING_OUT_TO_A_SOURCE0_PERFORM_FUNCTION__"),
                   ("UIKitCore", "__eventFetcherSourceCallback"), ("UIKitCore", "__processEventQueue"),
                   ("UIKitCore", "-[UIApplication sendEvent:]"), ("UIKitCore", "-[UIWindow sendEvent:]"),
                   ("UIKitCore", "-[UIWindow _sendTouchesForEvent:]"), ("UIKitCore", "-[UIControl touchesEnded:withEvent:]"),
                   ("UIKitCore", "-[UIControl _sendActionsForEvents:withEvent:]"),
                   ("UIKitCore", "-[UIApplication sendAction:to:from:forEvent:]")]


def stack(story, lob, screen, img):
    """Frames root first as (image, symbol)."""
    area, feed, cell, state = DOMAIN[lob]
    app, vc = img.app, screen
    if story == "result_store_wait":
        return _run_loop(img, "main_queue") + [
            (app, f"closure #1 in {vc}.reload()"), (app, f"{area}ResultStore.awaitRefresh(timeout:)"),
            ("libdispatch.dylib", "_dispatch_semaphore_wait_slow"), ("libdispatch.dylib", "_dispatch_sema4_wait"),
            ("libsystem_kernel.dylib", "semaphore_wait_trap")], "worker_lock"
    if story == "remote_config_value_read":
        return _run_loop(img, "layout") + [
            (app, f"{vc}.configureHeader(_:)"), (app, "FeatureFlags.isEnabled(_:)"),
            (app, "-[FIRRemoteConfig configValueForKey:]"), ("libdispatch.dylib", "_dispatch_sync_f_slow"),
            ("libdispatch.dylib", "__DISPATCH_WAIT_FOR_QUEUE__"), ("libdispatch.dylib", "_dispatch_thread_event_wait_slow"),
            ("libsystem_kernel.dylib", "__ulock_wait")], "worker_lock"
    if story == "token_store_contention":
        return _run_loop(img, "tap") + [
            (app, f"@objc {vc}.continueTapped(_:)"), (app, f"{vc}.continueTapped(_:)"),
            (app, "SessionHeaders.authorization.getter"), (app, "SessionTokenStore.currentToken.getter"),
            ("Foundation", "-[NSLock lock]"), ("libsystem_pthread.dylib", "_pthread_mutex_firstfit_lock_slow"),
            ("libsystem_pthread.dylib", "_pthread_mutex_firstfit_lock_wait"),
            ("libsystem_kernel.dylib", "__psynch_mutexwait")], "worker_lock"
    if story == "cache_migration":
        return _run_loop(img, "main_queue") + [
            (app, f"{vc}.viewDidLoad()"), (app, f"{feed}Repository.cachedItems()"), (app, "LocalCache.open()"),
            (app, "LocalCache.migrate(from:)"), ("libsqlite3.dylib", "sqlite3_exec"), ("libsqlite3.dylib", "sqlite3_step"),
            ("libsqlite3.dylib", "sqlite3VdbeExec"), ("libsqlite3.dylib", "sqlite3BtreeInsert"),
            ("libsqlite3.dylib", "balance"), ("libsqlite3.dylib", "pagerWalFrames"),
            ("libsystem_kernel.dylib", "pwrite")], "migration"
    if story == "defaults_large_write":
        return _run_loop(img, "tap") + [
            (app, f"@objc {vc}.saveTapped(_:)"), (app, f"{state}Store.persist(_:)"),
            ("Foundation", "-[NSUserDefaults(NSUserDefaults) setObject:forKey:]"),
            ("CoreFoundation", "_CFPreferencesSetAppValueWithContainerAndConfiguration"),
            ("CoreFoundation", "-[CFPrefsSource setValues:forKeys:count:copyValues:removeValuesForKeys:count:from:]"),
            ("CoreFoundation", "-[CFPrefsSource sendMessageSettingValues:forKeys:count:removingValuesForKeys:count:]"),
            ("libxpc.dylib", "xpc_connection_send_message_with_reply_sync"), ("libdispatch.dylib", "dispatch_mach_send_with_result_and_wait_for_reply"),
            ("libdispatch.dylib", "_dispatch_mach_send_and_wait_for_reply"),
            ("libsystem_kernel.dylib", "mach_msg"), ("libsystem_kernel.dylib", "mach_msg2_internal"),
            ("libsystem_kernel.dylib", "mach_msg2_trap")], "migration"
    if story == "cached_feed_decode":
        return _run_loop(img, "main_queue") + [
            (app, f"{vc}.restore()"), (app, f"{feed}Cache.read()"),
            ("Foundation", "JSONDecoder.decode<A>(_:from:)"), ("Foundation", "JSONDecoderImpl.unwrap<A>(_:as:for:_:)"),
            (app, f"protocol witness for Decodable.init(from:) in conformance {feed}"),
            ("Foundation", "JSONDecoderImpl.KeyedContainer.decode<A>(_:forKey:)"),
            ("Foundation", "JSONDecoderImpl.unwrapString(from:for:_:)"),
            ("Foundation", "JSONScanner.scanString()")], "migration"
    if story == "cell_date_formatting":
        return _run_loop(img, "layout") + [
            (app, f"closure #1 in {vc}.makeDataSource()"), (app, f"{cell}.configure(with:)"),
            (app, "DateLabels.relative(_:)"), ("Foundation", "-[NSDateFormatter init]"),
            ("CoreFoundation", "CFDateFormatterCreate"), ("CoreFoundation", "__ResetUDateFormat"),
            ("libicucore.A.dylib", "udat_open"), ("libicucore.A.dylib", "icu::SimpleDateFormat::SimpleDateFormat(icu::UnicodeString const&, icu::Locale const&, UErrorCode&)"),
            ("libicucore.A.dylib", "icu::DateFormatSymbols::initializeData(icu::Locale const&, char const*, UErrorCode&, signed char)")], "layout"
    if story == "image_decode_in_cell":
        return _run_loop(img, "layout") + [
            (app, f"closure #1 in {vc}.makeDataSource()"), (app, f"{cell}.configure(with:)"),
            (app, "LocalImageCache.image(for:)"), ("UIKitCore", "+[UIImage imageWithContentsOfFile:]"),
            ("UIKitCore", "-[UIImage initWithContentsOfFile:]"), ("ImageIO", "CGImageSourceCreateImageAtIndex"),
            ("ImageIO", "IIOImageSource::createImageAtIndex(unsigned long, IIODictionary*, int*)"),
            ("ImageIO", "IIOReadPlugin::callDecodeImage(IIODecodeParameter*, IIOImageRef*, ...)"),
            ("AppleJPEG", "applejpeg_decode_image_all")], "layout"
    raise ValueError(f"unknown hang story {story}")


def choose(family, rng):
    if any(family in v for v in STORIES.values()):
        return family
    return rng.choice(STORIES.get(family) or sum(STORIES.values(), ()))


def sample(story, lob, screen, img, rng):
    """Attributes of the one sample event the SDK attaches to a hang."""
    frames_root_first, _ = stack(story, lob, screen, img)
    frames = [img.frame(image, symbol) for image, symbol in reversed(frames_root_first)]
    encoded = base64.b64encode(json.dumps(frames, separators=(",", ":")).encode()).decode()
    return {"emb.type": "perf.thread_blockage_sample", "sample_overhead": rng.randint(60, 900) * 1000,
            "frame_count": len(frames), "emb.stacktrace.ios": encoded}


def decode(encoded):
    return json.loads(base64.b64decode(encoded))
