"""Main-thread stack samples for Android thread blockages (Cortex XCOR RUM's ANR signal).

SDK behavior (embrace-android-sdk 9.3.1, embrace-android-instrumentation-anr):
- BlockedThreadDetector posts a heartbeat to the main Looper every ``sampleIntervalMs`` (100 ms). Once the main
  thread has not answered for ``minDurationMs`` (1000 ms) the interval starts at the last answered heartbeat and
  every following watchdog tick captures ``Thread.getStackTrace()`` of the main thread (ThreadStacktraceSampler).
- At most 80 samples per interval carry a stack (DEFAULT_MAX_STACKTRACES_PER_INTERVAL); later samples carry
  ``sample_code`` 1 and no thread data. Stacks are cut to 200 lines; ``frame_count`` is the untruncated depth.
- ThreadBlockageSampler omits the thread data of a sample whose stack equals the previous one; the backend
  (go/lib/codec/session_anr.go) reuses the previous stack for such samples.
- ThreadBlockageOtelMapper: event ``perf.thread_blockage_sample`` with ``emb.type``, ``sample_overhead`` (ns),
  ``sample_code``, ``jvm.thread.state``, ``thread_priority``, ``frame_count`` and ``exception.stacktrace`` (lines
  joined by ``\\n``, innermost first, ``StackTraceElement.toString()``). Completed intervals carry no
  ``last_known_time_unix_nano``. All times are whole milliseconds.

The backend keeps only the first sample, the "best" sample (middle sample of the largest non-system package) and an
ad-SDK sample of each interval, and colors frames by package prefix (go/lib/common/category_jvm.go). So the flame
graph gets its width from different root causes across ANRs and from how each blockage evolves over time.
"""
import hashlib

SAMPLE_INTERVAL_MS = 100
THRESHOLD_MS = 1000
MAX_STACKS = 80
MAX_SAMPLES = 1000
FRAME_LIMIT = 200
MAIN_PRIORITY = 5

# Cause families accepted by the ScenarioSpec (spec/scenario.schema.json blockage_cause) and their stories.
FAMILIES = {
    "worker_lock": ("result_store_wait", "token_store_contention", "remote_config_cache_read", "run_blocking_snapshot"),
    "migration": ("cache_migration", "cache_query", "preferences_commit", "cached_feed_parse"),
    "layout": ("list_bind_formatting", "image_decode_in_layout", "sponsored_webview_init", "boarding_pass_render"),
}
# Stories that only make sense for one line of business.
STORY_LOB = {"sponsored_webview_init": "ecommerce", "boarding_pass_render": "travel"}
STORY_WEIGHTS = {
    "ecommerce": {"result_store_wait": 3, "token_store_contention": 2, "remote_config_cache_read": 2, "run_blocking_snapshot": 1,
                  "cache_migration": 2, "cache_query": 1, "preferences_commit": 3, "cached_feed_parse": 3,
                  "list_bind_formatting": 2, "image_decode_in_layout": 3, "sponsored_webview_init": 3},
    "banking": {"result_store_wait": 2, "token_store_contention": 3, "remote_config_cache_read": 2, "run_blocking_snapshot": 2,
                "cache_migration": 3, "cache_query": 2, "preferences_commit": 2, "cached_feed_parse": 2,
                "list_bind_formatting": 4, "image_decode_in_layout": 1},
    "travel": {"result_store_wait": 2, "token_store_contention": 2, "remote_config_cache_read": 2, "run_blocking_snapshot": 2,
               "cache_migration": 2, "cache_query": 2, "preferences_commit": 1, "cached_feed_parse": 3,
               "list_bind_formatting": 1, "image_decode_in_layout": 2, "boarding_pass_render": 3},
}
# Per line of business: the cached data each screen reads, its store and the list it renders.
DOMAIN = {
    "ecommerce": {"pkg": "ecommerce", "data": "catalog", "entity": "Product", "store": "BagStore", "feed": "ProductFeed",
                  "list": "ProductGridAdapter", "db": "CatalogCacheDatabase", "dao": "ProductDao", "state": "BagState"},
    "banking": {"pkg": "banking", "data": "accounts", "entity": "Transaction", "store": "AccountSnapshotStore",
                "feed": "TransactionFeed", "list": "TransactionHistoryAdapter", "db": "AccountCacheDatabase",
                "dao": "TransactionDao", "state": "TransferDraft"},
    "travel": {"pkg": "travel", "data": "trips", "entity": "Itinerary", "store": "TripStore", "feed": "FareFeed",
               "list": "ItineraryAdapter", "db": "TripCacheDatabase", "dao": "ItineraryDao", "state": "BookingDraft"},
}
FIREBASE_CONFIG = "com.google.firebase:firebase-config@@22.1.0"
GMS_ADS = "com.google.android.gms:play-services-ads@@23.6.0"
GMS_ADS_LITE = "com.google.android.gms:play-services-ads-lite@@23.6.0"


def _h(*parts):
    return int(hashlib.md5(":".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()[:8], 16)


class Frames:
    """Builds StackTraceElement.toString() lines; framework line numbers follow the device's OS build."""

    def __init__(self, api, manufacturer, package):
        self.api, self.oem, self.package = str(api), (manufacturer or "").lower(), package

    def fw(self, method, line, file=None):
        """AOSP framework frame: the line moves between API levels and OEM builds, stable per build."""
        if self.api != "35":
            line += (_h(method, self.api) % 41) - 12
        if self.oem == "samsung":
            line += _h(method, "oem") % 23
        return f"{method}({file or self._file(method, 'java')}:{max(20, line)})"

    @staticmethod
    def lib(method, line, file=None, ext="kt"):
        """Library frame pinned by the app's dependency version."""
        return f"{method}({file or Frames._file(method, ext)}:{line})"

    @staticmethod
    def gms(method, artifact, line):
        """Play services and Firebase frames carry the artifact coordinates instead of a file name."""
        return f"{method}({artifact}:{line})"

    @staticmethod
    def native(method):
        return f"{method}(Native Method)"

    def app(self, method, line):
        return f"{self.package}.{method}({self._file(method, 'kt')}:{line})"

    @staticmethod
    def _file(method, ext):
        cls = method.rsplit(".", 1)[0].rsplit(".", 1)[-1].split("$")[0]
        if ext == "kt" and cls.endswith("Kt") and len(cls) > 2:
            cls = cls[:-2]
        return f"{cls}.{ext}"


# --- main-thread entry paths, root first -------------------------------------------------------------------

def looper(f):
    return [f.fw("com.android.internal.os.ZygoteInit.main", 886),
            f.fw("com.android.internal.os.RuntimeInit$MethodAndArgsCaller.run", 580, "RuntimeInit.java"),
            f.native("java.lang.reflect.Method.invoke"),
            f.fw("android.app.ActivityThread.main", 8705),
            f.fw("android.os.Looper.loop", 317),
            f.fw("android.os.Looper.loopOnce", 232, "Looper.java")]


def handler(f):
    return looper(f) + [f.fw("android.os.Handler.dispatchMessage", 99), f.fw("android.os.Handler.handleCallback", 959)]


def coroutine(f):
    """A coroutine resumed on Dispatchers.Main (HandlerContext posts DispatchedTask)."""
    return handler(f) + [f.lib("kotlinx.coroutines.DispatchedTask.run", 108),
                         f.lib("kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith", 33, "ContinuationImpl.kt")]


def choreographer(f):
    return handler(f) + [f.fw("android.view.Choreographer$FrameDisplayEventReceiver.run", 1536, "Choreographer.java"),
                         f.fw("android.view.Choreographer.doFrame", 1430),
                         f.fw("android.view.Choreographer.doCallbacks", 1105),
                         f.fw("android.view.Choreographer$CallbackRecord.run", 1545, "Choreographer.java")]


def recompose(f, screen):
    """Compose recomposition driven by the frame clock, into the screen's composable."""
    return choreographer(f) + [
        f.lib("androidx.compose.ui.platform.AndroidUiFrameClock$withFrameNanos$2$callback$1.doFrame", 39,
              "AndroidUiFrameClock.android.kt"),
        f.lib("androidx.compose.runtime.BroadcastFrameClock.sendFrame", 71),
        f.lib("androidx.compose.runtime.Recomposer$runRecomposeAndApplyChanges$2.invokeSuspend", 615, "Recomposer.kt"),
        f.lib("kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith", 33, "ContinuationImpl.kt"),
        f.lib("kotlinx.coroutines.DispatchedTask.run", 108),
        f.lib("androidx.compose.ui.platform.AndroidUiDispatcher.performTrampolineDispatch", 81,
              "AndroidUiDispatcher.android.kt"),
        f.lib("androidx.compose.runtime.Recomposer.performRecompose", 1375, "Recomposer.kt"),
        f.lib("androidx.compose.runtime.CompositionImpl.recompose", 1040, "Composition.kt"),
        f.lib("androidx.compose.runtime.ComposerImpl.recompose$runtime_release", 3159, "Composer.kt"),
        f.lib("androidx.compose.runtime.ComposerImpl.doCompose", 3783, "Composer.kt"),
        f.lib("androidx.compose.runtime.ComposerImpl.recomposeToGroupEnd", 2881, "Composer.kt"),
        f.lib("androidx.compose.runtime.RecomposeScopeImpl.compose", 196),
        f.lib("androidx.compose.runtime.internal.ComposableLambdaImpl.invoke", 121, "ComposableLambda.kt"),
        f.app(f"{screen.pkg}.ui.{screen.name}ScreenKt.{screen.name}Content", screen.line(1, 90, 260))]


def traversal(f):
    return choreographer(f) + [f.fw("android.view.ViewRootImpl$TraversalRunnable.run", 11399, "ViewRootImpl.java"),
                               f.fw("android.view.ViewRootImpl.doTraversal", 3221),
                               f.fw("android.view.ViewRootImpl.performTraversals", 4558)]


def view_list_layout(f, adapter, line):
    """A legacy RecyclerView screen laying out new rows (the layout pass binds view holders)."""
    frames = traversal(f) + [f.fw("android.view.ViewRootImpl.performLayout", 4934),
                             f.fw("android.view.View.layout", 25804)]
    for _ in range(3):
        frames += [f.fw("android.view.ViewGroup.layout", 7799), f.fw("android.widget.FrameLayout.onLayout", 330),
                   f.fw("android.widget.FrameLayout.layoutChildren", 375, "FrameLayout.java"),
                   f.fw("android.view.View.layout", 25804)]
    return frames + [
        f.lib("androidx.recyclerview.widget.RecyclerView.onLayout", 4577, ext="java"),
        f.lib("androidx.recyclerview.widget.RecyclerView.dispatchLayout", 4520, ext="java"),
        f.lib("androidx.recyclerview.widget.RecyclerView.dispatchLayoutStep2", 4797, ext="java"),
        f.lib("androidx.recyclerview.widget.LinearLayoutManager.onLayoutChildren", 668, ext="java"),
        f.lib("androidx.recyclerview.widget.LinearLayoutManager.fill", 1591, ext="java"),
        f.lib("androidx.recyclerview.widget.LinearLayoutManager.layoutChunk", 1632, ext="java"),
        f.lib("androidx.recyclerview.widget.LinearLayoutManager$LayoutState.next", 2340, "LinearLayoutManager.java"),
        f.lib("androidx.recyclerview.widget.RecyclerView$Recycler.getViewForPosition", 6732, "RecyclerView.java"),
        f.lib("androidx.recyclerview.widget.RecyclerView$Recycler.tryGetViewHolderForPositionByDeadline", 7038,
              "RecyclerView.java"),
        f.lib("androidx.recyclerview.widget.RecyclerView$Recycler.tryBindViewHolderByDeadline", 6553, "RecyclerView.java"),
        f.lib("androidx.recyclerview.widget.RecyclerView$Adapter.bindViewHolder", 7764, "RecyclerView.java"),
        f.lib("androidx.recyclerview.widget.RecyclerView$Adapter.onBindViewHolder", 7724, "RecyclerView.java"),
        f.app(f"{adapter}.onBindViewHolder", line)]


def compose_lazy_layout(f, screen):
    """A LazyColumn composing and measuring newly visible items during the layout pass."""
    return traversal(f) + [
        f.fw("android.view.ViewRootImpl.performMeasure", 4747),
        f.fw("android.view.View.measure", 28215),
        f.lib("androidx.compose.ui.platform.AndroidComposeView.onMeasure", 1917, "AndroidComposeView.android.kt"),
        f.lib("androidx.compose.ui.node.MeasureAndLayoutDelegate.measureAndLayout", 362),
        f.lib("androidx.compose.ui.node.MeasureAndLayoutDelegate.remeasureAndRelayoutIfNeeded", 518),
        f.lib("androidx.compose.ui.node.LayoutNode.remeasure-_Sx5XlM$ui_release", 1253, "LayoutNode.kt"),
        f.lib("androidx.compose.ui.node.MeasurePassDelegate.remeasure-BRTryo0", 655, "MeasurePassDelegate.kt"),
        f.lib("androidx.compose.ui.node.OwnerSnapshotObserver.observeMeasureSnapshotReads$ui_release", 133,
              "OwnerSnapshotObserver.kt"),
        f.lib("androidx.compose.foundation.lazy.LazyListKt$rememberLazyListMeasurePolicy$1$1.invoke-0kLqBqw", 183,
              "LazyList.kt"),
        f.lib("androidx.compose.foundation.lazy.LazyListMeasureKt.measureLazyList-x0Ok8Vo", 210, "LazyListMeasure.kt"),
        f.lib("androidx.compose.foundation.lazy.LazyListMeasuredItemProvider.getAndMeasure-0kLqBqw", 48,
              "LazyListMeasuredItemProvider.kt"),
        f.lib("androidx.compose.foundation.lazy.layout.LazyLayoutMeasureScopeImpl.measure-0kLqBqw", 143,
              "LazyLayoutMeasureScope.kt"),
        f.lib("androidx.compose.ui.layout.LayoutNodeSubcompositionsState.subcompose", 509, "SubcomposeLayout.kt"),
        f.lib("androidx.compose.runtime.CompositionImpl.setContent", 632, "Composition.kt"),
        f.lib("androidx.compose.runtime.ComposerImpl.composeContent$runtime_release", 3122, "Composer.kt"),
        f.lib("androidx.compose.runtime.ComposerImpl.doCompose", 3783, "Composer.kt"),
        f.lib("androidx.compose.runtime.internal.ComposableLambdaImpl.invoke", 121, "ComposableLambda.kt"),
        f.app(f"{screen.pkg}.ui.{screen.name}ScreenKt${screen.name}List$1$1.invoke", screen.line(2, 120, 300))]


def tap(f, screen, action):
    """A Compose click: input dispatch through Embrace's Window.Callback wrapper into the click lambda."""
    frames = looper(f) + [
        f.fw("android.os.MessageQueue.next", 342), f.native("android.os.MessageQueue.nativePollOnce"),
        f.fw("android.view.InputEventReceiver.dispatchInputEvent", 326),
        f.fw("android.view.ViewRootImpl$WindowInputEventReceiver.onInputEvent", 11469, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl.enqueueInputEvent", 11134),
        f.fw("android.view.ViewRootImpl.doProcessInputEvents", 11178),
        f.fw("android.view.ViewRootImpl.deliverInputEvent", 11208),
        f.fw("android.view.ViewRootImpl$InputStage.deliver", 8095, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$InputStage.onDeliverToNext", 8122, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$InputStage.forward", 8126, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$AsyncInputStage.forward", 8343, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$InputStage.apply", 8134, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$ViewPostImeInputStage.onProcess", 8778, "ViewRootImpl.java"),
        f.fw("android.view.ViewRootImpl$ViewPostImeInputStage.processPointerEvent", 8835, "ViewRootImpl.java"),
        f.fw("android.view.View.dispatchPointerEvent", 16723),
        f.fw("com.android.internal.policy.DecorView.dispatchTouchEvent", 497),
        f.lib("io.embrace.android.embracesdk.internal.instrumentation.compose.tap.EmbraceWindowCallback.dispatchTouchEvent", 32),
        f.fw("android.app.Activity.dispatchTouchEvent", 4466),
        f.fw("com.android.internal.policy.PhoneWindow.superDispatchTouchEvent", 2008),
        f.fw("com.android.internal.policy.DecorView.superDispatchTouchEvent", 547)]
    for _ in range(5):
        frames += [f.fw("android.view.ViewGroup.dispatchTouchEvent", 3150),
                   f.fw("android.view.ViewGroup.dispatchTransformedTouchEvent", 3466)]
    return frames + [
        f.lib("androidx.compose.ui.platform.AndroidComposeView.dispatchTouchEvent", 1683, "AndroidComposeView.android.kt"),
        f.lib("androidx.compose.ui.platform.AndroidComposeView.handleMotionEvent-8iAsVTc", 1750,
              "AndroidComposeView.android.kt"),
        f.lib("androidx.compose.ui.platform.AndroidComposeView.sendMotionEvent-8iAsVTc", 1846,
              "AndroidComposeView.android.kt"),
        f.lib("androidx.compose.ui.input.pointer.PointerInputEventProcessor.process-BIzXfog", 140, "PointerInputEventProcessor.kt"),
        f.lib("androidx.compose.ui.input.pointer.HitPathTracker.dispatchChanges", 152),
        f.lib("androidx.compose.ui.input.pointer.NodeParent.dispatchMainEventPass", 376, "HitPathTracker.kt"),
        f.lib("androidx.compose.ui.input.pointer.Node.dispatchMainEventPass", 588, "HitPathTracker.kt"),
        f.lib("androidx.compose.ui.input.pointer.SuspendingPointerInputModifierNodeImpl.onPointerEvent-H0pRuoY", 634,
              "SuspendingPointerInputFilter.kt"),
        f.lib("androidx.compose.ui.input.pointer.SuspendingPointerInputModifierNodeImpl$PointerEventHandlerCoroutine.offerPointerEvent",
              750, "SuspendingPointerInputFilter.kt"),
        f.lib("kotlinx.coroutines.CancellableContinuationImpl.resumeWith", 380, "CancellableContinuationImpl.kt"),
        f.lib("kotlinx.coroutines.DispatchedTaskKt.resume", 235, "DispatchedTask.kt"),
        f.lib("kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith", 33, "ContinuationImpl.kt"),
        f.lib("androidx.compose.foundation.gestures.TapGestureDetectorKt$detectTapAndPress$2$1.invokeSuspend", 255,
              "TapGestureDetector.kt"),
        f.lib("androidx.compose.foundation.ClickableNode$clickPointerInput$3.invoke-k-4lQ0M", 639, "Clickable.kt"),
        f.app(f"{screen.pkg}.ui.{screen.name}ScreenKt${screen.name}Content${screen.index(3, 2, 9)}$1.invoke",
              screen.line(3, 140, 320)),
        f.app(f"{screen.pkg}.ui.{screen.name}ViewModel.{action}", screen.line(4, 60, 190))]


def fragment_view_created(f, screen):
    """A pending fragment transaction creating the screen's view (FragmentManager posts execPendingActions)."""
    return handler(f) + [
        f.lib("androidx.fragment.app.FragmentManager$5.run", 1813, "FragmentManager.java"),
        f.lib("androidx.fragment.app.FragmentManager.execPendingActions", 2064, ext="java"),
        f.lib("androidx.fragment.app.FragmentManager.removeRedundantOperationsAndExecute", 2180, ext="java"),
        f.lib("androidx.fragment.app.FragmentManager.executeOpsTogether", 2280, ext="java"),
        f.lib("androidx.fragment.app.FragmentManager.moveToState", 1586, ext="java"),
        f.lib("androidx.fragment.app.FragmentStateManager.moveToExpectedState", 279, ext="java"),
        f.lib("androidx.fragment.app.FragmentStateManager.createView", 579, ext="java"),
        f.lib("androidx.fragment.app.Fragment.performViewCreated", 3141, ext="java"),
        f.app(f"{screen.pkg}.ui.{screen.name}Fragment.onViewCreated", screen.line(5, 48, 110))]


class Screen:
    """The screen the user is on, named the way the app's source tree names it."""

    def __init__(self, lob, fragment, seed):
        self.lob, self.pkg = lob, DOMAIN[lob]["pkg"]
        self.name = (fragment or "Home").replace("Fragment", "") or "Home"
        self.seed = seed

    def line(self, slot, lo, hi):
        return lo + _h(self.pkg, self.name, slot) % (hi - lo)

    def index(self, slot, lo, hi):
        return lo + _h(self.pkg, self.name, slot, "i") % (hi - lo)


# --- leaves ------------------------------------------------------------------------------------------------

def park(f, timed=True):
    return [f.lib("java.util.concurrent.locks.LockSupport.parkNanos" if timed else "java.util.concurrent.locks.LockSupport.park",
                  269 if timed else 221, ext="java"),
            f.native("jdk.internal.misc.Unsafe.park")]


def latch_wait(f):
    return [f.lib("java.util.concurrent.CountDownLatch.await", 278, ext="java"),
            f.lib("java.util.concurrent.locks.AbstractQueuedSynchronizer.tryAcquireSharedNanos", 1188, ext="java"),
            f.lib("java.util.concurrent.locks.AbstractQueuedSynchronizer.acquire", 790, ext="java")] + park(f)


def sqlite_write(f):
    """SQLiteDatabase.execSQL runs every statement through SQLiteStatement.executeUpdateDelete."""
    return [f.lib("androidx.sqlite.db.framework.FrameworkSQLiteDatabase.execSQL", 245),
            f.fw("android.database.sqlite.SQLiteDatabase.execSQL", 2221),
            f.fw("android.database.sqlite.SQLiteDatabase.executeSql", 2345),
            f.fw("android.database.sqlite.SQLiteStatement.executeUpdateDelete", 69),
            f.fw("android.database.sqlite.SQLiteSession.executeForChangedRowCount", 887),
            f.fw("android.database.sqlite.SQLiteConnection.executeForChangedRowCount", 894),
            f.native("android.database.sqlite.SQLiteConnection.nativeExecuteForChangedRowCount")]


def cursor_fill(f):
    return [f.fw("android.database.AbstractCursor.moveToNext", 278),
            f.fw("android.database.AbstractCursor.moveToPosition", 249),
            f.fw("android.database.sqlite.SQLiteCursor.onMove", 120),
            f.fw("android.database.sqlite.SQLiteCursor.fillWindow", 147),
            f.fw("android.database.sqlite.SQLiteQuery.fillWindow", 67),
            f.fw("android.database.sqlite.SQLiteSession.executeForCursorWindow", 1038),
            f.fw("android.database.sqlite.SQLiteConnection.executeForCursorWindow", 1123),
            f.native("android.database.sqlite.SQLiteConnection.nativeExecuteForCursorWindow")]


def moshi_object(f, depth):
    frames = [f.lib("com.squareup.moshi.internal.NullSafeJsonAdapter.fromJson", 41, ext="java")]
    for _ in range(depth):
        frames += [f.lib("com.squareup.moshi.kotlin.reflect.KotlinJsonAdapter.fromJson", 105),
                   f.lib("com.squareup.moshi.internal.NullSafeJsonAdapter.fromJson", 41, ext="java"),
                   f.lib("com.squareup.moshi.CollectionJsonAdapter.fromJson", 81, ext="java"),
                   f.lib("com.squareup.moshi.CollectionJsonAdapter$2.fromJson", 54, "CollectionJsonAdapter.java"),
                   f.lib("com.squareup.moshi.internal.NullSafeJsonAdapter.fromJson", 41, ext="java")]
    return frames + [f.lib("com.squareup.moshi.kotlin.reflect.KotlinJsonAdapter.fromJson", 97)]


# --- stories -----------------------------------------------------------------------------------------------
# Each story returns phases: (share of the blockage, thread state, [stack variants root first], change rate).
# A change rate of 0 holds one stack for the phase (the SDK then dedupes the repeated samples).

def story_phases(story, f, screen, rng):
    d = DOMAIN[screen.lob]
    app, pkg = f.app, screen.pkg
    if story == "result_store_wait":
        base = recompose(f, screen) + [app(f"{pkg}.data.ResultStore.awaitRefresh", 94)]
        waiting = base +[f.lib("java.lang.Object.wait", 405, ext="java"), f.native("java.lang.Object.wait")]
        recheck = base[:-1] + [app(f"{pkg}.data.ResultStore.awaitRefresh", 99), app(f"{pkg}.data.ResultStore.isStale", 131),
                               f.lib("java.util.concurrent.ConcurrentHashMap.get", 950, ext="java")]
        return [(1.0, "TIMED_WAITING", [waiting] * 11 + [recheck], .18)]
    if story == "token_store_contention":
        stack = tap(f, screen, "onContinueClicked") + [
            app(f"core.network.SessionHeaders.authorization", 37),
            app(f"core.auth.SessionTokenStore.currentToken", 58)]
        return [(1.0, "BLOCKED", [stack], 0)]
    if story == "remote_config_cache_read":
        def read(call_site):
            return recompose(f, screen) + [
                app(f"{pkg}.ui.{screen.name}ScreenKt.{screen.name}Header", screen.line(6, 40, 90) + call_site),
                app("core.flags.FeatureFlags.isEnabled", 41),
                f.gms("com.google.firebase.remoteconfig.FirebaseRemoteConfig.getBoolean", FIREBASE_CONFIG, 1),
                f.gms("com.google.firebase.remoteconfig.internal.ConfigGetParameterHandler.getBoolean", FIREBASE_CONFIG, 2),
                f.gms("com.google.firebase.remoteconfig.internal.ConfigGetParameterHandler.getConfigsFromCache", FIREBASE_CONFIG, 1),
                f.gms("com.google.firebase.remoteconfig.internal.ConfigCacheClient.getBlocking", FIREBASE_CONFIG, 3),
                f.gms("com.google.firebase.remoteconfig.internal.ConfigCacheClient.await", FIREBASE_CONFIG, 2),
                f.gms("com.google.firebase.remoteconfig.internal.ConfigCacheClient$AwaitListener.await", FIREBASE_CONFIG, 1),
            ] + latch_wait(f)
        # ConfigCacheClient.getBlocking waits up to 5 seconds per read; a second flag read waits again.
        return [(.5, "TIMED_WAITING", [read(0)], 0), (.5, "TIMED_WAITING", [read(14)], 0)]
    if story == "run_blocking_snapshot":
        stack = coroutine(f) + [
            app(f"{pkg}.ui.{screen.name}ViewModel$refresh$1.invokeSuspend", screen.line(7, 60, 140)),
            app(f"{pkg}.data.{d['feed']}Repository.cachedSnapshot", 88),
            f.lib("kotlinx.coroutines.BuildersKt.runBlocking", 1, "Unknown Source"),
            f.lib("kotlinx.coroutines.BuildersKt__BuildersKt.runBlocking", 69, "Builders.kt"),
            f.lib("kotlinx.coroutines.BlockingCoroutine.joinBlocking", 136, "Builders.kt")] + park(f)
        return [(1.0, "TIMED_WAITING", [stack], 0)]
    if story == "cache_migration":
        open_db = fragment_view_created(f, screen) + [
            app(f"{pkg}.data.{d['feed']}Repository.cachedItems", 52),
            app(f"{pkg}.data.cache.{d['dao']}_Impl.recent", 118),
            f.lib("androidx.room.util.DBUtil.query", 88),
            f.lib("androidx.room.RoomDatabase.query", 563),
            f.lib("androidx.room.RoomDatabase.getOpenHelper", 1, "Unknown Source"),
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteOpenHelper.getWritableDatabase", 104),
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteOpenHelper$OpenHelper.getSupportDatabase", 232,
                  "FrameworkSQLiteOpenHelper.kt"),
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteOpenHelper$OpenHelper.getWritableOrReadableDatabase", 254,
                  "FrameworkSQLiteOpenHelper.kt"),
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteOpenHelper$OpenHelper.innerGetDatabase", 297,
                  "FrameworkSQLiteOpenHelper.kt"),
            f.fw("android.database.sqlite.SQLiteOpenHelper.getWritableDatabase", 419),
            f.fw("android.database.sqlite.SQLiteOpenHelper.getDatabaseLocked", 513),
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteOpenHelper$OpenHelper.onUpgrade", 403,
                  "FrameworkSQLiteOpenHelper.kt"),
            f.lib("androidx.room.RoomOpenHelper.onUpgrade", 104)]
        migrate = open_db + [app(f"{pkg}.data.cache.CacheMigrations$MIGRATION_41_42$1.migrate", 0)]

        def step(line):
            return migrate[:-1] + [app(f"{pkg}.data.cache.CacheMigrations$MIGRATION_41_42$1.migrate", line)] + sqlite_write(f)
        # CREATE TABLE, INSERT ... SELECT copy, DROP and RENAME, then the two index builds.
        return [(.06, "RUNNABLE", [step(63)], 0), (.62, "RUNNABLE", [step(66)], 0),
                (.04, "RUNNABLE", [step(70)], 0), (.28, "RUNNABLE", [step(72), step(73)], .3)]
    if story == "cache_query":
        query = recompose(f, screen) + [
            app(f"{pkg}.ui.{screen.name}ViewModel.visibleItems", screen.line(8, 70, 160)),
            app(f"{pkg}.data.cache.{d['dao']}_Impl.search", 214),
            f.lib("androidx.room.util.DBUtil.query", 88)]
        fill = query + [f.lib("androidx.room.RoomDatabase.query", 595)] + [
            f.lib("androidx.sqlite.db.framework.FrameworkSQLiteDatabase.query", 199),
            f.fw("android.database.sqlite.SQLiteDatabase.rawQueryWithFactory", 1771),
            f.fw("android.database.sqlite.SQLiteDirectCursorDriver.query", 50),
            f.fw("android.database.sqlite.SQLiteCursor.getCount", 155),
            f.fw("android.database.sqlite.SQLiteCursor.fillWindow", 147),
            f.fw("android.database.sqlite.SQLiteQuery.fillWindow", 67),
            f.fw("android.database.sqlite.SQLiteSession.executeForCursorWindow", 1038),
            f.fw("android.database.sqlite.SQLiteConnection.executeForCursorWindow", 1123),
            f.native("android.database.sqlite.SQLiteConnection.nativeExecuteForCursorWindow")]
        rows = query[:-1] + [app(f"{pkg}.data.cache.{d['dao']}_Impl.search", 231)]
        read_rows = [rows + cursor_fill(f),
                     rows + [f.fw("android.database.AbstractWindowedCursor.getString", 47),
                             f.fw("android.database.CursorWindow.getString", 469),
                             f.native("android.database.CursorWindow.nativeGetString")],
                     rows[:-1] + [app(f"{pkg}.data.cache.{d['dao']}_Impl.search", 244),
                                  app(f"{pkg}.data.cache.Converters.toMoney", 31),
                                  f.lib("java.math.BigDecimal.<init>", 925, ext="java")]]
        return [(.55, "RUNNABLE", [fill], 0), (.45, "RUNNABLE", read_rows, .55)]
    if story == "preferences_commit":
        commit = tap(f, screen, "onSaveForLater") + [
            app(f"core.state.{d['state']}Store.persist", 46),
            f.fw("android.app.SharedPreferencesImpl$EditorImpl.commit", 604, "SharedPreferencesImpl.java"),
            f.lib("android.app.SharedPreferencesImpl.-$$Nest$menqueueDiskWrite", 0, "Unknown Source"),
            f.fw("android.app.SharedPreferencesImpl.enqueueDiskWrite", 691),
            f.fw("android.app.SharedPreferencesImpl$2.run", 672, "SharedPreferencesImpl.java"),
            f.lib("android.app.SharedPreferencesImpl.-$$Nest$mwriteToFile", 0, "Unknown Source"),
            f.fw("android.app.SharedPreferencesImpl.writeToFile", 807)]
        xml = commit + [f.fw("com.android.internal.util.XmlUtils.writeMapXml", 246),
                        f.fw("com.android.internal.util.XmlUtils.writeMapXml", 270),
                        f.fw("com.android.internal.util.XmlUtils.writeValueXml", 791)]
        serialize = [xml + [f.fw("com.android.internal.util.FastXmlSerializer.text", 386),
                            f.fw("com.android.internal.util.FastXmlSerializer.escapeAndAppendString", 166),
                            f.fw("com.android.internal.util.FastXmlSerializer.append", 101)],
                     xml + [f.fw("com.android.internal.util.FastXmlSerializer.text", 386),
                            f.fw("com.android.internal.util.FastXmlSerializer.escapeAndAppendString", 172),
                            f.fw("com.android.internal.util.FastXmlSerializer.append", 89),
                            f.fw("com.android.internal.util.FastXmlSerializer.flushBytes", 222),
                            f.fw("java.io.FileOutputStream.write", 391),
                            f.fw("libcore.io.IoBridge.write", 547),
                            f.fw("libcore.io.BlockGuardOs.write", 450),
                            f.native("libcore.io.Linux.writeBytes")]]
        sync = commit[:-1] + [f.fw("android.app.SharedPreferencesImpl.writeToFile", 829),
                              f.fw("android.os.FileUtils.sync", 266), f.native("java.io.FileDescriptor.sync")]
        return [(.38, "RUNNABLE", serialize, .5), (.62, "RUNNABLE", [sync], 0)]
    if story == "cached_feed_parse":
        load = coroutine(f) + [app(f"{pkg}.ui.{screen.name}ViewModel$restore$1.invokeSuspend", screen.line(9, 50, 120)),
                               app(f"{pkg}.data.{d['feed']}Cache.read", 77)]
        adapter = load + [f.lib("com.squareup.moshi.Moshi.adapter", 152, ext="java"),
                          f.lib("com.squareup.moshi.Moshi.adapter", 114, ext="java"),
                          f.lib("com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory.create", 235),
                          f.lib("kotlin.reflect.full.KClasses.getPrimaryConstructor", 46),
                          f.lib("kotlin.reflect.jvm.internal.KClassImpl.getConstructors", 220),
                          f.lib("kotlin.reflect.jvm.internal.KClassImpl$Data.getConstructors", 136, "KClassImpl.kt"),
                          f.lib("kotlin.reflect.jvm.internal.ReflectProperties$LazySoftVal.invoke", 70, "ReflectProperties.java"),
                          f.lib("kotlin.reflect.jvm.internal.KClassImpl$Data$constructors$2.invoke", 92, "KClassImpl.kt"),
                          f.lib("kotlin.reflect.jvm.internal.impl.descriptors.runtime.components.ReflectKotlinClass$Factory.create",
                                34, "ReflectKotlinClass.kt")]
        parse = load + [app(f"{pkg}.data.{d['feed']}Cache.decode", 91),
                        f.lib("com.squareup.moshi.JsonAdapter.fromJson", 70, ext="java")]
        parsing = [parse + moshi_object(f, depth) + leaf for depth in (1, 2, 3) for leaf in (
            [f.lib("com.squareup.moshi.JsonUtf8Reader.nextString", 815, ext="java"),
             f.lib("okio.Buffer.readUtf8", 1234), f.lib("okio.internal._Utf8Kt.commonToUtf8String", 92, "-Utf8.kt")],
            [f.lib("com.squareup.moshi.JsonUtf8Reader.peek", 118, ext="java"),
             f.lib("com.squareup.moshi.JsonUtf8Reader.doPeek", 293, ext="java"),
             f.lib("okio.RealBufferedSource.request", 211)],
            [f.lib("com.squareup.moshi.kotlin.reflect.KotlinJsonAdapter$Binding.set", 299, "KotlinJsonAdapter.kt"),
             f.lib("kotlin.reflect.jvm.internal.KMutableProperty1Impl.set", 79),
             f.lib("kotlin.reflect.jvm.internal.calls.CallerImpl$Method.callMethod", 97, "CallerImpl.kt"),
             f.native("java.lang.reflect.Method.invoke")])]
        mapping = load[:-1] + [app(f"{pkg}.data.{d['feed']}Cache.read", 82), app(f"{pkg}.data.{d['entity']}Mapper.toUi", 64),
                               f.lib("java.text.NumberFormat.format", 349, ext="java"),
                               f.lib("android.icu.text.DecimalFormat.format", 1157, ext="java")]
        return [(.18, "RUNNABLE", [adapter], 0), (.7, "RUNNABLE", parsing, .8), (.12, "RUNNABLE", [mapping], 0)]
    if story == "list_bind_formatting":
        bind = view_list_layout(f, f"{pkg}.ui.{d['list']}", 88)
        holder = bind[:-1] + [app(f"{pkg}.ui.{d['list']}.onBindViewHolder", 91),
                              app(f"{pkg}.ui.{d['list']}$RowHolder.bind", 142)]
        variants = [
            holder + [app(f"core.format.DateLabels.relative", 27), f.lib("java.text.SimpleDateFormat.<init>", 593, ext="java"),
                      f.lib("java.text.SimpleDateFormat.initialize", 640, ext="java"),
                      f.lib("java.text.DateFormatSymbols.getInstanceRef", 382, ext="java"),
                      f.lib("libcore.icu.ICU.getDateFormatSymbols", 1, "Unknown Source")],
            holder + [app(f"core.format.MoneyFormat.display", 44), f.lib("java.text.NumberFormat.getCurrencyInstance", 467, ext="java"),
                      f.lib("java.text.NumberFormat.getInstance", 1119, ext="java"),
                      f.lib("java.text.DecimalFormat.<init>", 571, ext="java"),
                      f.lib("android.icu.text.DecimalFormat.applyPattern", 1898, ext="java")],
            holder + [f.fw("android.widget.TextView.setText", 6902), f.fw("android.widget.TextView.checkForRelayout", 10851),
                      f.fw("android.widget.TextView.makeNewLayout", 9937), f.fw("android.widget.TextView.makeSingleLayout", 10046),
                      f.fw("android.text.StaticLayout$Builder.build", 470), f.fw("android.text.StaticLayout.<init>", 745),
                      f.fw("android.text.StaticLayout.generate", 821), f.fw("android.text.MeasuredParagraph.buildForStaticLayout", 668),
                      f.native("android.graphics.text.MeasuredText$Builder.nBuildMeasuredText")],
            bind[:-1] + [app(f"{pkg}.ui.{d['list']}.onBindViewHolder", 96),
                         f.lib("androidx.constraintlayout.widget.ConstraintLayout.onMeasure", 1594, ext="java"),
                         f.lib("androidx.constraintlayout.core.widgets.analyzer.BasicMeasure.solverMeasure", 278, ext="java"),
                         f.lib("androidx.constraintlayout.core.widgets.ConstraintWidgetContainer.layout", 817, ext="java"),
                         f.lib("androidx.constraintlayout.core.LinearSystem.minimize", 650, ext="java")]]
        return [(1.0, "RUNNABLE", variants, .85)]
    if story == "image_decode_in_layout":
        item = compose_lazy_layout(f, screen) + [app(f"{pkg}.ui.{d['entity']}CardKt.{d['entity']}Card", screen.line(10, 40, 110)),
                                                 app("core.image.LocalImageCache.bitmap", 58)]
        decode = [item + [f.fw("android.graphics.ImageDecoder.decodeBitmap", 1944),
                          f.fw("android.graphics.ImageDecoder.decodeBitmapImpl", 1886),
                          f.native("android.graphics.ImageDecoder.nDecodeBitmap")],
                  item + [f.fw("android.graphics.ImageDecoder.decodeBitmap", 1944),
                          f.fw("android.graphics.ImageDecoder.createFromFile", 1151),
                          f.fw("libcore.io.IoBridge.open", 574), f.fw("libcore.io.BlockGuardOs.open", 274),
                          f.native("libcore.io.Linux.open")],
                  item[:-1] + [app("core.image.LocalImageCache.bitmap", 64), app("core.image.LocalImageCache.scaleToFit", 102),
                               f.fw("android.graphics.Bitmap.createScaledBitmap", 905),
                               f.fw("android.graphics.Bitmap.createBitmap", 1015),
                               f.fw("android.graphics.Canvas.drawBitmap", 1673),
                               f.native("android.graphics.BaseCanvas.nDrawBitmap")]]
        return [(1.0, "RUNNABLE", decode, .7)]
    if story == "sponsored_webview_init":
        banner = recompose(f, screen) + [
            f.lib("androidx.compose.ui.viewinterop.AndroidView_androidKt$AndroidView$1$1.invoke", 113, "AndroidView.android.kt"),
            app(f"{pkg}.sponsored.SponsoredBannerKt$SponsoredBanner$1$1.invoke", 52),
            f.gms("com.google.android.gms.ads.BaseAdView.loadAd", GMS_ADS_LITE, 22),
            f.gms("com.google.android.gms.internal.ads.zzbfx.zzc", GMS_ADS, 1),
            f.gms("com.google.android.gms.internal.ads.zzcgd.zza", GMS_ADS, 3)]
        provider = banner + [f.fw("android.webkit.WebView.<init>", 466), f.fw("android.webkit.WebView.ensureProviderCreated", 3170),
                             f.fw("android.webkit.WebView.getFactory", 3164), f.fw("android.webkit.WebViewFactory.getProvider", 381)]
        load_class = provider + [f.fw("android.webkit.WebViewFactory.getProviderClass", 490),
                                 f.fw("java.lang.ClassLoader.loadClass", 637), f.fw("dalvik.system.BaseDexClassLoader.findClass", 259),
                                 f.fw("dalvik.system.DexPathList.findClass", 571), f.fw("dalvik.system.DexFile.loadClassBinaryName", 379),
                                 f.native("dalvik.system.DexFile.defineClassNative")]
        load_lib = provider + [f.fw("android.webkit.WebViewFactory.getProviderClass", 504),
                               f.fw("android.webkit.WebViewLibraryLoader.loadNativeLibrary", 149),
                               f.fw("java.lang.Runtime.nativeLoad", 1102, "Runtime.java"),
                               f.native("java.lang.Runtime.nativeLoad")]
        start = banner + [f.fw("android.webkit.WebView.<init>", 466),
                          f.lib("com.android.webview.chromium.WebViewChromium.init", 344, ext="java"),
                          f.lib("com.android.webview.chromium.WebViewChromiumAwInit.startYourEngines", 456, ext="java"),
                          f.lib("com.android.webview.chromium.WebViewChromiumAwInit.startChromiumLocked", 342, ext="java"),
                          f.lib("org.chromium.android_webview.AwBrowserProcess.start", 129, ext="java"),
                          f.native("J.N.MbKIeU_6")]
        return [(.35, "RUNNABLE", [load_class], 0), (.25, "RUNNABLE", [load_lib], 0), (.4, "RUNNABLE", [start], 0)]
    if story == "boarding_pass_render":
        card = recompose(f, screen) + [app(f"{pkg}.trips.BoardingPassCardKt.BoardingPassCard", 77),
                                       app(f"{pkg}.trips.BarcodeRenderer.render", 33)]
        encode = card + [f.lib("com.google.zxing.MultiFormatWriter.encode", 99, ext="java"),
                         f.lib("com.google.zxing.pdf417.PDF417Writer.encode", 69, ext="java"),
                         f.lib("com.google.zxing.pdf417.encoder.PDF417.generateBarcodeLogic", 657, ext="java")]
        pixels = card[:-1] + [app(f"{pkg}.trips.BarcodeRenderer.render", 38), app(f"{pkg}.trips.BarcodeRenderer.toBitmap", 61)]
        paint = [pixels + [f.fw("android.graphics.Bitmap.setPixel", 2233), f.native("android.graphics.Bitmap.nativeSetPixel")],
                 pixels + [f.fw("android.graphics.Bitmap.setPixel", 2229), f.fw("android.graphics.Bitmap.checkPixelAccess", 2042)],
                 pixels + [f.lib("com.google.zxing.common.BitMatrix.get", 118, ext="java")],
                 pixels[:-1] + [app(f"{pkg}.trips.BarcodeRenderer.toBitmap", 64)]]
        return [(.12, "RUNNABLE", [encode], 0), (.88, "RUNNABLE", paint, .9)]
    raise ValueError(f"unknown blockage story {story}")


def choose_story(family, lob, rng):
    """A spec may name a family (worker_lock, migration, layout) or one story; otherwise pick by line of business."""
    weights = STORY_WEIGHTS[lob]
    if family in weights:
        return family
    names = [n for n in (FAMILIES.get(family) or tuple(weights)) if STORY_LOB.get(n, lob) == lob and n in weights]
    total = sum(weights[n] for n in names)
    x = rng.uniform(0, total)
    for n in names:
        x -= weights[n]
        if x <= 0:
            return n
    return names[-1]


def family_of(story):
    return next(k for k, v in FAMILIES.items() if story in v)


def blockage_samples(rng, story, screen, frames, start_ms, end_ms):
    """Watchdog ticks after the 1 s threshold, capturing the stack that is running at each tick."""
    phases = story_phases(story, frames, screen, rng)
    duration = end_ms - start_ms
    tick = start_ms + rng.randint(0, SAMPLE_INTERVAL_MS - 1)
    while tick <= start_ms + THRESHOLD_MS:
        tick += SAMPLE_INTERVAL_MS
    samples, last, held = [], None, {}
    t = tick
    while t < end_ms and len(samples) < MAX_SAMPLES:
        elapsed, acc = (t - start_ms) / duration, 0.0
        for i, (share, state, variants, change) in enumerate(phases):
            acc += share
            if elapsed <= acc or i == len(phases) - 1:
                break
        if i not in held or (change and rng.random() < change):
            held[i] = rng.choice(variants)
        stack = held[i][::-1]
        overhead = rng.choices((0, 1, 2, 3), (60, 30, 7, 3))[0]
        samples.append((t, state, stack, overhead))
        t += SAMPLE_INTERVAL_MS + (rng.randint(0, 2) if rng.random() < .9 else rng.randint(5, 25))
    events = []
    for n, (t, state, stack, overhead) in enumerate(samples):
        a = {"emb.type": "perf.thread_blockage_sample", "sample_overhead": overhead * 1_000_000}
        if n >= MAX_STACKS:
            a["sample_code"] = 1
        else:
            a["sample_code"] = 0
            current = (state, tuple(stack))
            if current != last:
                a.update({"jvm.thread.state": state, "thread_priority": MAIN_PRIORITY, "frame_count": len(stack),
                          "exception.stacktrace": "\n".join(stack[:FRAME_LIMIT])})
            last = current
        events.append((t, a))
    return events


def first_stack(events):
    return next((a["exception.stacktrace"].split("\n") for _, a in events if "exception.stacktrace" in a), [])
