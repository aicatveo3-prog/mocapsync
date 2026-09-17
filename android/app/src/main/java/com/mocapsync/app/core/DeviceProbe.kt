package com.mocapsync.app.core

import android.content.Context
import android.content.pm.PackageManager
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.StreamConfigurationMap
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import android.util.Range
import android.util.Size
import com.mocapsync.app.BuildConfig
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 기기 진단.
 *
 * 중요: 이 클래스는 **카메라 권한을 요구하지 않습니다.**
 * CameraManager.getCameraCharacteristics() 는 카메라를 열지 않고 메타데이터만 읽기 때문에
 * android.permission.CAMERA 없이 호출할 수 있습니다.
 *
 * 그래서 1단계(녹화 기능이 전혀 없는 상태)에서도 다음을 미리 확인할 수 있습니다:
 *   - SENSOR_INFO_TIMESTAMP_SOURCE 가 REALTIME 인가 UNKNOWN 인가  <- 3단계 설계를 가르는 값
 *   - 1080p 에서 60fps 이상이 가능한가
 *   - OIS / EIS 를 끌 수 있는가
 *   - AE / AWB LOCK 을 지원하는가
 *   - MANUAL_SENSOR 로 셔터를 1/500 이하로 직접 고정할 수 있는가
 *
 * 정직하게 밝히는 한계:
 *   여기서 읽는 값은 "기기가 신고하는 능력치"입니다. 실제로 CameraX 세션을 열었을 때
 *   60fps 가 유지되는지, OIS OFF 요청이 실제로 반영되는지는 3단계에서 카메라를 열고
 *   확인해야 최종 확정됩니다. 이 화면은 사전 스크리닝입니다.
 *
 * 상수 참조 규칙:
 *   Key 는 CameraCharacteristics.*, "값" 상수는 선언 클래스인 CameraMetadata.* 로 씁니다.
 *   (Java static 멤버를 하위 클래스 이름으로 접근하는 건 컴파일러 구현에 의존적이라 피합니다)
 */
object DeviceProbe {

    enum class CheckStatus { PASS, WARN, FAIL, UNKNOWN }

    data class Check(
        val name: String,
        val status: CheckStatus,
        val detail: String
    )

    data class CameraReport(
        val id: String,
        val facing: String,
        val info: List<Pair<String, String>>,
        val checks: List<Check>
    )

    data class ClockFacts(
        val elapsedRealtimeNanos: Long,
        val uptimeNanos: Long,
        val deltaNanos: Long,
        /** true 면 SystemClock.uptimeNanos()(API 31+) 를 썼다는 뜻. false 면 ms 해상도 근사치. */
        val uptimeIsNanoPrecise: Boolean,
        val wallClockMillis: Long
    )

    data class FullReport(
        val app: List<Pair<String, String>>,
        val device: List<Pair<String, String>>,
        val clock: ClockFacts,
        val cameras: List<CameraReport>,
        val errors: List<String>
    )

    // ── 클럭 ──────────────────────────────────────────────────────────────────

    /**
     * elapsedRealtimeNanos 와 uptimeNanos 의 차이를 잽니다.
     *
     * 왜 중요한가: 카메라의 SENSOR_TIMESTAMP 가 UNKNOWN 소스인 기기에서는
     * 타임스탬프가 uptime(= 절전 중 멈추는 시계) 기준입니다.
     * 우리 클럭 동기는 elapsedRealtime(= 절전 중에도 흐르는 시계) 기준이므로
     * 두 시계의 델타를 알아야 변환이 가능합니다.
     *
     * 이 델타는 기기가 절전에 들어갈 때마다 커집니다. 즉 상수가 아닙니다.
     * 그래서 녹화 세션마다 다시 재야 합니다.
     */
    fun clockFacts(): ClockFacts {
        val er = SystemClock.elapsedRealtimeNanos()
        val precise = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S // API 31
        val up = if (precise) {
            SystemClock.uptimeNanos()
        } else {
            SystemClock.uptimeMillis() * 1_000_000L
        }
        return ClockFacts(
            elapsedRealtimeNanos = er,
            uptimeNanos = up,
            deltaNanos = er - up,
            uptimeIsNanoPrecise = precise,
            wallClockMillis = System.currentTimeMillis()
        )
    }

    // ── 전체 리포트 ────────────────────────────────────────────────────────────

    fun probe(context: Context): FullReport {
        val errors = mutableListOf<String>()

        val app = buildList {
            add("packageName" to context.packageName)
            var vName = "?"
            var vCode = "?"
            try {
                val pi = context.packageManager.getPackageInfo(context.packageName, 0)
                vName = pi.versionName ?: "?"
                vCode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    pi.longVersionCode.toString()
                } else {
                    @Suppress("DEPRECATION")
                    pi.versionCode.toString()
                }
            } catch (e: PackageManager.NameNotFoundException) {
                errors += "패키지 정보 조회 실패: ${e.message}"
            }
            add("versionName" to vName)
            add("versionCode" to vCode)
            add("git commit" to BuildConfig.GIT_SHA)
            add("빌드시각" to BuildConfig.BUILD_STAMP)
            add("CI run #" to BuildConfig.CI_RUN.toString())
        }

        val device = listOf(
            "제조사" to Build.MANUFACTURER,
            "브랜드" to Build.BRAND,
            "모델" to Build.MODEL,
            "기기코드" to Build.DEVICE,
            "하드웨어" to Build.HARDWARE,
            "Android" to "${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
            "빌드ID" to Build.DISPLAY,
            "ABI" to Build.SUPPORTED_ABIS.joinToString(", ")
        )

        val cameras = mutableListOf<CameraReport>()
        try {
            val cm = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            for (id in cm.cameraIdList) {
                try {
                    cameras += describeCamera(cm, id)
                } catch (t: Throwable) {
                    errors += "카메라 $id 조회 실패: ${t.javaClass.simpleName}: ${t.message}"
                }
            }
        } catch (t: Throwable) {
            errors += "카메라 목록 조회 실패: ${t.javaClass.simpleName}: ${t.message}"
        }

        return FullReport(
            app = app,
            device = device,
            clock = clockFacts(),
            cameras = cameras,
            errors = errors
        )
    }

    // ── 카메라 1개 분석 ────────────────────────────────────────────────────────

    private fun describeCamera(cm: CameraManager, id: String): CameraReport {
        val ch = cm.getCameraCharacteristics(id)

        val facingRaw = ch.get(CameraCharacteristics.LENS_FACING)
        val facing = when (facingRaw) {
            CameraMetadata.LENS_FACING_FRONT -> "전면"
            CameraMetadata.LENS_FACING_BACK -> "후면"
            CameraMetadata.LENS_FACING_EXTERNAL -> "외장"
            else -> "알수없음($facingRaw)"
        }

        val hwLevelRaw = ch.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)
        val hwLevel = when (hwLevelRaw) {
            CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
            CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
            CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
            CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
            CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
            else -> "알수없음($hwLevelRaw)"
        }

        val tsSourceRaw = ch.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE)
        val tsSource = when (tsSourceRaw) {
            CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME -> "REALTIME"
            CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_UNKNOWN -> "UNKNOWN"
            else -> "알수없음($tsSourceRaw)"
        }

        val caps = ch.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES)?.toList() ?: emptyList()
        val hasManualSensor =
            caps.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR)
        val hasHighSpeed =
            caps.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO)
        val hasReadSensorSettings =
            caps.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_READ_SENSOR_SETTINGS)

        val aeLock = ch.get(CameraCharacteristics.CONTROL_AE_LOCK_AVAILABLE)
        val awbLock = ch.get(CameraCharacteristics.CONTROL_AWB_LOCK_AVAILABLE)

        val oisModes = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION)
            ?.toList() ?: emptyList()
        val eisModes = ch.get(CameraCharacteristics.CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES)
            ?.toList() ?: emptyList()
        val afModes = ch.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)?.toList() ?: emptyList()

        val aeFpsRanges: List<Range<Int>> =
            ch.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)?.toList() ?: emptyList()
        val maxAeFps = aeFpsRanges.maxOfOrNull { it.upper } ?: 0
        val hasFixed60 = aeFpsRanges.any { it.lower >= 60 && it.upper >= 60 }

        val expRange = ch.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
        val isoRange = ch.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
        val focal = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.toList() ?: emptyList()
        val physSize = ch.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)

        val map: StreamConfigurationMap? = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)

        val recSizes: List<Size> = map?.getOutputSizes(MediaRecorder::class.java)?.toList() ?: emptyList()
        val texSizes: List<Size> = map?.getOutputSizes(SurfaceTexture::class.java)?.toList() ?: emptyList()

        val size1080 = Size(1920, 1080)
        val fps1080Rec = minFrameDurationToFps(map, MediaRecorder::class.java, size1080, recSizes)
        val fps1080Tex = minFrameDurationToFps(map, SurfaceTexture::class.java, size1080, texSizes)
        val best1080 = listOfNotNull(fps1080Rec, fps1080Tex).maxOrNull()

        val size720 = Size(1280, 720)
        val fps720 = listOfNotNull(
            minFrameDurationToFps(map, MediaRecorder::class.java, size720, recSizes),
            minFrameDurationToFps(map, SurfaceTexture::class.java, size720, texSizes)
        ).maxOrNull()

        val highSpeed = map?.highSpeedVideoFpsRanges?.toList() ?: emptyList()

        val physicalIds: Set<String> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            try {
                ch.physicalCameraIds
            } catch (t: Throwable) {
                emptySet()
            }
        } else {
            emptySet()
        }

        // ── 사람이 읽는 정보 ──
        val info = buildList {
            add("카메라 ID" to id)
            add("방향" to facing)
            add("하드웨어 레벨" to hwLevel)
            add("SENSOR_TIMESTAMP 소스" to "$tsSource (raw=$tsSourceRaw)")
            add("MANUAL_SENSOR" to yn(hasManualSensor))
            add("READ_SENSOR_SETTINGS" to yn(hasReadSensorSettings))
            add("고속촬영(슬로모) 지원" to yn(hasHighSpeed))
            add("AE LOCK" to nullableYn(aeLock))
            add("AWB LOCK" to nullableYn(awbLock))
            add("OIS 모드" to oisModes.joinToString { oisName(it) }.ifEmpty { "없음(OIS 미탑재)" })
            add("EIS(비디오 안정화) 모드" to eisModes.joinToString { eisName(it) }.ifEmpty { "없음" })
            add("AF 모드" to afModes.joinToString())
            add("AE 목표 FPS 범위" to aeFpsRanges.joinToString { "[${it.lower},${it.upper}]" }.ifEmpty { "없음" })
            add("1080p 최대 FPS(추정)" to (best1080?.let { fmt1(it) } ?: "산출불가"))
            add("  - MediaRecorder 경로" to (fps1080Rec?.let { fmt1(it) } ?: "미보고"))
            add("  - SurfaceTexture 경로" to (fps1080Tex?.let { fmt1(it) } ?: "미보고"))
            add("720p 최대 FPS(추정)" to (fps720?.let { fmt1(it) } ?: "산출불가"))
            add("고속촬영 FPS 범위" to highSpeed.joinToString { "[${it.lower},${it.upper}]" }.ifEmpty { "없음" })
            add("녹화 가능 해상도 수" to "${recSizes.size}개")
            add(
                "최대 녹화 해상도" to (recSizes.maxByOrNull { it.width.toLong() * it.height }
                    ?.let { "${it.width}x${it.height}" } ?: "없음")
            )
            add("1080p 지원" to yn(recSizes.any { it.width == 1920 && it.height == 1080 }))
            add(
                "노출시간 범위" to (expRange?.let { r ->
                    val shortest = if (r.lower > 0) "1/${1_000_000_000L / r.lower}초" else "?"
                    "${r.lower}ns ~ ${r.upper}ns  (최단 = $shortest)"
                } ?: "미보고(MANUAL_SENSOR 없음)")
            )
            add("ISO 범위" to (isoRange?.let { "${it.lower} ~ ${it.upper}" } ?: "미보고"))
            add("초점거리" to focal.joinToString { "${it}mm" })
            add("센서 물리크기" to (physSize?.let { "${it.width}x${it.height}mm" } ?: "미보고"))
            if (physicalIds.isNotEmpty()) {
                add("논리카메라 구성" to physicalIds.joinToString())
            }
        }

        // ── 판정 ──
        val checks = buildList {
            add(
                Check(
                    name = "프레임 타임스탬프 소스",
                    status = when (tsSourceRaw) {
                        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME -> CheckStatus.PASS
                        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_UNKNOWN -> CheckStatus.WARN
                        else -> CheckStatus.UNKNOWN
                    },
                    detail = when (tsSourceRaw) {
                        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME ->
                            "REALTIME. SENSOR_TIMESTAMP 에 클럭 오프셋만 더하면 됩니다. 최상의 경우."
                        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_UNKNOWN ->
                            "UNKNOWN. uptime 기준이라 (elapsedRealtime - uptime) 델타로 변환해야 합니다. " +
                                "동작은 가능하지만 변환 오차 관리가 필요합니다."
                        else -> "값을 읽지 못했습니다."
                    }
                )
            )
            add(
                Check(
                    name = "60fps 고정 가능 (AE 범위)",
                    status = when {
                        hasFixed60 -> CheckStatus.PASS
                        maxAeFps >= 60 -> CheckStatus.WARN
                        else -> CheckStatus.FAIL
                    },
                    detail = when {
                        hasFixed60 -> "[60,60] 같은 고정 범위가 있습니다. 프레임 간격이 일정해집니다."
                        maxAeFps >= 60 ->
                            "상한 ${maxAeFps}fps 는 있으나 하한이 낮습니다(가변 프레임). " +
                                "어두우면 자동으로 30fps 로 떨어질 수 있어 조명이 중요합니다."
                        else -> "AE 최대 ${maxAeFps}fps. Pose2Sim 권장(60Hz 이상)을 만족하기 어렵습니다."
                    }
                )
            )
            add(
                Check(
                    name = "1080p 60fps 이상 (설정맵 추정)",
                    status = when {
                        best1080 == null -> CheckStatus.UNKNOWN
                        best1080 >= 59.0 -> CheckStatus.PASS
                        else -> CheckStatus.WARN
                    },
                    detail = when {
                        best1080 == null ->
                            "기기가 최소 프레임 간격을 보고하지 않습니다. 흔한 일이며 3단계 실측으로 확인합니다."
                        best1080 >= 59.0 -> "약 ${fmt1(best1080)}fps"
                        else -> "약 ${fmt1(best1080)}fps. 720p 는 ${fps720?.let { fmt1(it) } ?: "?"}fps."
                    }
                )
            )
            add(
                Check(
                    name = "OIS 끄기 가능",
                    status = when {
                        oisModes.isEmpty() -> CheckStatus.PASS
                        oisModes.contains(CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF) -> CheckStatus.PASS
                        else -> CheckStatus.FAIL
                    },
                    detail = when {
                        oisModes.isEmpty() -> "OIS 미탑재. 항상 꺼진 상태이므로 오히려 캘리브레이션에 유리합니다."
                        oisModes.contains(CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF) ->
                            "OFF 모드 선택 가능."
                        else -> "OFF 를 지원하지 않습니다. 렌즈가 프레임마다 움직여 내부 파라미터가 변합니다."
                    }
                )
            )
            add(
                Check(
                    name = "EIS(전자식 흔들림보정) 끄기 가능",
                    status = when {
                        eisModes.isEmpty() -> CheckStatus.PASS
                        eisModes.contains(CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF) -> CheckStatus.PASS
                        else -> CheckStatus.FAIL
                    },
                    detail = if (eisModes.isEmpty()) {
                        "EIS 미지원(= 항상 OFF)."
                    } else {
                        "지원 모드: ${eisModes.joinToString { eisName(it) }}"
                    }
                )
            )
            add(
                Check(
                    name = "AE / AWB 잠금",
                    status = if (aeLock == true && awbLock == true) CheckStatus.PASS else CheckStatus.WARN,
                    detail = "AE LOCK=${nullableYn(aeLock)}, AWB LOCK=${nullableYn(awbLock)}. " +
                        "노출/화이트밸런스가 촬영 중 변하면 2D 검출이 흔들립니다."
                )
            )
            add(
                Check(
                    name = "셔터 1/500초 이하 직접 지정",
                    status = when {
                        !hasManualSensor -> CheckStatus.WARN
                        expRange == null -> CheckStatus.UNKNOWN
                        expRange.lower <= 2_000_000L -> CheckStatus.PASS
                        else -> CheckStatus.WARN
                    },
                    detail = when {
                        !hasManualSensor ->
                            "MANUAL_SENSOR 미지원. 셔터를 직접 못 정하므로 AE 잠금 + 밝은 조명으로 대응합니다."
                        expRange == null -> "노출 범위를 읽지 못했습니다."
                        expRange.lower <= 2_000_000L ->
                            "최단 ${expRange.lower}ns = 1/${1_000_000_000L / expRange.lower}초. 모션블러 억제 가능."
                        else -> "최단 ${expRange.lower}ns. 1/500초(2,000,000ns)보다 깁니다."
                    }
                )
            )
            add(
                Check(
                    name = "카메라2 하드웨어 레벨",
                    status = when (hwLevelRaw) {
                        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> CheckStatus.FAIL
                        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> CheckStatus.WARN
                        else -> CheckStatus.PASS
                    },
                    detail = "$hwLevel. LEGACY 면 프레임 타임스탬프 신뢰도가 낮아 이 프로젝트에 부적합합니다."
                )
            )
        }

        return CameraReport(id = id, facing = facing, info = info, checks = checks)
    }

    // ── 유틸 ──────────────────────────────────────────────────────────────────

    private fun minFrameDurationToFps(
        map: StreamConfigurationMap?,
        klass: Class<*>,
        size: Size,
        available: List<Size>
    ): Double? {
        if (map == null) return null
        if (available.none { it.width == size.width && it.height == size.height }) return null
        return try {
            val ns = map.getOutputMinFrameDuration(klass, size)
            if (ns > 0) 1_000_000_000.0 / ns else null
        } catch (t: Throwable) {
            null
        }
    }

    private fun oisName(v: Int) = when (v) {
        CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF -> "OFF(0)"
        CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_ON -> "ON(1)"
        else -> "기타($v)"
    }

    private fun eisName(v: Int) = when (v) {
        CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF -> "OFF(0)"
        CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_ON -> "ON(1)"
        2 -> "PREVIEW_STABILIZATION(2)"
        else -> "기타($v)"
    }

    private fun yn(b: Boolean) = if (b) "예" else "아니오"

    private fun nullableYn(b: Boolean?) = when (b) {
        true -> "예"
        false -> "아니오"
        null -> "미보고"
    }

    private fun fmt1(d: Double) = String.format(Locale.US, "%.1f", d)

    // ── 텍스트 리포트 (로그 파일 머리말로 사용) ────────────────────────────────

    fun reportText(context: Context): String {
        val r = probe(context)
        val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US).format(Date())
        return buildString {
            appendLine("========================================================")
            appendLine(" MocapSync 기기 진단 리포트")
            appendLine(" 생성시각(기기 로컬): $stamp")
            appendLine("========================================================")
            appendLine()
            appendLine("[앱]")
            r.app.forEach { appendLine("  ${it.first} = ${it.second}") }
            appendLine()
            appendLine("[기기]")
            r.device.forEach { appendLine("  ${it.first} = ${it.second}") }
            appendLine()
            appendLine("[클럭]")
            appendLine("  elapsedRealtimeNanos = ${r.clock.elapsedRealtimeNanos}")
            appendLine(
                "  uptimeNanos          = ${r.clock.uptimeNanos}" +
                    if (r.clock.uptimeIsNanoPrecise) "" else "   (API<31 이므로 ms 해상도 근사)"
            )
            appendLine(
                "  delta (er - up)      = ${r.clock.deltaNanos} ns = " +
                    "${String.format(Locale.US, "%.3f", r.clock.deltaNanos / 1_000_000.0)} ms"
            )
            appendLine("  System.currentTimeMillis = ${r.clock.wallClockMillis}")
            appendLine("  * delta 는 기기가 절전에 들어갈 때마다 커집니다. 상수가 아닙니다.")
            appendLine()
            r.cameras.forEach { c ->
                appendLine("---- 카메라 ${c.id} (${c.facing}) ----")
                c.info.forEach { appendLine("  ${it.first} = ${it.second}") }
                appendLine("  판정:")
                c.checks.forEach { appendLine("    [${it.status}] ${it.name} :: ${it.detail}") }
                appendLine()
            }
            if (r.errors.isNotEmpty()) {
                appendLine("[오류]")
                r.errors.forEach { appendLine("  $it") }
                appendLine()
            }
        }
    }
}
