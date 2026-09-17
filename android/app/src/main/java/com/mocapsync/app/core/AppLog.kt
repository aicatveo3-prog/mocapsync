package com.mocapsync.app.core

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale

/**
 * 앱 전역 로그 버퍼.
 *
 * 왜 필요한가:
 * 이 프로젝트는 개발자가 실기기 테스트를 직접 할 수 없는 구조입니다.
 * (코드 작성 -> CI 빌드 -> 사용자가 설치/실행 -> 로그 첨부 -> 수정)
 * 그래서 logcat 이 아니라 "앱 안에서 읽고 파일로 뽑을 수 있는 로그"가 1일차부터 필요합니다.
 *
 * 스레드 안전:
 *   write() 는 synchronized 로 보호됩니다. SNTP 스레드/카메라 콜백에서 호출해도 안전합니다.
 *
 * 성능:
 *   로그 한 줄마다 List 를 복사하지 않습니다. [revision] 만 올리고,
 *   UI 가 recomposition 시점에 [snapshot] 을 한 번 호출합니다.
 *   (3단계에서 프레임마다 로그를 남길 때를 대비한 구조)
 */
object AppLog {

    private const val LOGCAT_TAG = "MocapSync"
    private const val MAX_LINES = 4000

    private val timeFmt = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private val buffer = ArrayDeque<String>()

    private val _revision = MutableStateFlow(0)

    /** 로그가 바뀔 때마다 증가. Compose 에서 이 값을 관찰하고 [snapshot] 을 읽으세요. */
    val revision: StateFlow<Int> = _revision.asStateFlow()

    fun d(tag: String, msg: String) = write('D', tag, msg, null)
    fun i(tag: String, msg: String) = write('I', tag, msg, null)
    fun w(tag: String, msg: String) = write('W', tag, msg, null)
    fun e(tag: String, msg: String, t: Throwable? = null) = write('E', tag, msg, t)

    /** 사용자가 "지금 이 순간"을 표시하고 싶을 때 (예: 스톱워치 촬영 시작 직전) */
    fun mark(msg: String) = write('M', "MARK", "──────── $msg ────────", null)

    private fun write(level: Char, tag: String, msg: String, t: Throwable?) {
        val now = Date()
        val head = "${timeFmt.format(now)} $level/$tag: $msg"
        val line = if (t == null) head else head + "\n" + Log.getStackTraceString(t)

        synchronized(buffer) {
            while (buffer.size >= MAX_LINES) buffer.pollFirst()
            buffer.addLast(line)
        }
        _revision.value = _revision.value + 1

        // logcat 에도 남깁니다 (USB 디버깅이 가능한 상황에서는 이게 더 편함)
        when (level) {
            'E' -> Log.e(LOGCAT_TAG, "$tag: $msg", t)
            'W' -> Log.w(LOGCAT_TAG, "$tag: $msg")
            else -> Log.i(LOGCAT_TAG, "$tag: $msg")
        }
    }

    fun snapshot(): List<String> = synchronized(buffer) { buffer.toList() }

    fun size(): Int = synchronized(buffer) { buffer.size }

    fun clear() {
        synchronized(buffer) { buffer.clear() }
        _revision.value = _revision.value + 1
    }
}
