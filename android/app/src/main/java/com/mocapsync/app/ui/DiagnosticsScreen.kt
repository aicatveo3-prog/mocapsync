package com.mocapsync.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mocapsync.app.core.AppLog
import com.mocapsync.app.core.DeviceProbe
import com.mocapsync.app.core.LogExporter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Locale

@Composable
fun DiagnosticsScreen() {
    val context = LocalContext.current
    var refreshKey by remember { mutableStateOf(0) }

    val report by produceState<DeviceProbe.FullReport?>(initialValue = null, refreshKey) {
        value = withContext(Dispatchers.Default) {
            val r = DeviceProbe.probe(context)
            AppLog.i(
                "Diag",
                "진단 완료: 카메라 ${r.cameras.size}대, 오류 ${r.errors.size}건, " +
                    "clockDelta=${r.clock.deltaNanos}ns"
            )
            r
        }
    }

    val r = report
    if (r == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                CircularProgressIndicator()
                Spacer(Modifier.height(12.dp))
                Text("카메라 특성 읽는 중...", fontSize = 13.sp)
            }
        }
        return
    }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        VerdictCard(r)

        SectionCard(title = "클럭") {
            KeyValueRow("elapsedRealtimeNanos", r.clock.elapsedRealtimeNanos.toString())
            KeyValueRow(
                "uptimeNanos",
                r.clock.uptimeNanos.toString() +
                    if (r.clock.uptimeIsNanoPrecise) "" else "  (ms 근사)"
            )
            KeyValueRow(
                "델타 (er - up)",
                "${r.clock.deltaNanos} ns = ${
                    String.format(Locale.US, "%.3f", r.clock.deltaNanos / 1_000_000.0)
                } ms"
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "이 델타는 카메라 타임스탬프가 UNKNOWN(=uptime 기준)일 때 변환에 씁니다. " +
                    "폰이 절전에 들어갈 때마다 커지므로 상수로 캐시하면 안 됩니다.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        SectionCard(title = "기기") {
            r.device.forEach { KeyValueRow(it.first, it.second) }
        }

        SectionCard(title = "앱 / 빌드") {
            r.app.forEach { KeyValueRow(it.first, it.second) }
        }

        r.cameras.forEach { cam -> CameraCard(cam) }

        if (r.errors.isNotEmpty()) {
            SectionCard(title = "오류") {
                r.errors.forEach {
                    Text(
                        it,
                        fontSize = 12.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.error
                    )
                }
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(
                onClick = { LogExporter.share(context) },
                modifier = Modifier.weight(1f)
            ) { Text("이 리포트 공유", fontWeight = FontWeight.Bold) }
            OutlinedButton(
                onClick = { LogExporter.copyToClipboard(context) },
                modifier = Modifier.weight(1f)
            ) { Text("복사") }
        }
        OutlinedButton(
            onClick = { refreshKey++ },
            modifier = Modifier.fillMaxWidth()
        ) { Text("다시 측정") }

        Spacer(Modifier.height(24.dp))
    }
}

/** 후면 카메라 중 가장 조건이 좋은 것을 기준으로 "이 폰 쓸 수 있나"를 한 줄로 답합니다. */
@Composable
private fun VerdictCard(r: DeviceProbe.FullReport) {
    val backCams = r.cameras.filter { it.facing == "후면" }
    val target = backCams.firstOrNull() ?: r.cameras.firstOrNull()

    val checks = target?.checks ?: emptyList()
    val failCount = checks.count { it.status == DeviceProbe.CheckStatus.FAIL }
    val warnCount = checks.count { it.status == DeviceProbe.CheckStatus.WARN }

    val (label, color, msg) = when {
        target == null -> Triple(
            "판정 불가", StatusColors.unknown,
            "카메라 정보를 하나도 읽지 못했습니다. 로그를 보내주세요."
        )
        failCount > 0 -> Triple(
            "제약 있음", StatusColors.fail,
            "$failCount 개 항목이 '불가'입니다. 아래에서 어떤 항목인지 확인하고 로그를 보내주세요. " +
                "해상도를 낮추거나 카메라 ID를 바꿔서 우회할 수 있는 경우가 많습니다."
        )
        warnCount > 0 -> Triple(
            "사용 가능 (주의 $warnCount)", StatusColors.warn,
            "치명적 문제는 없습니다. '주의' 항목은 촬영 조건(조명/설정)으로 보완하거나 " +
                "3단계에서 실측으로 확정합니다."
        )
        else -> Triple(
            "양호", StatusColors.pass,
            "주 후면 카메라가 모든 항목을 통과했습니다."
        )
    }

    Column(
        Modifier
            .fillMaxWidth()
            .background(color.copy(alpha = 0.12f), RoundedCornerShape(14.dp))
            .padding(16.dp)
    ) {
        Text("종합 판정", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(label, fontSize = 28.sp, fontWeight = FontWeight.Black, color = color)
        Spacer(Modifier.height(6.dp))
        Text(msg, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface)
        if (target != null) {
            Spacer(Modifier.height(8.dp))
            Text(
                "기준 카메라: ID ${target.id} (${target.facing}) · 감지된 카메라 ${r.cameras.size}대",
                fontSize = 12.sp,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
private fun CameraCard(cam: DeviceProbe.CameraReport) {
    var expanded by remember { mutableStateOf(false) }

    SectionCard(title = "카메라 ${cam.id} · ${cam.facing}") {
        cam.checks.forEach { CheckRow(it) }
        Spacer(Modifier.height(4.dp))
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        TextButton(onClick = { expanded = !expanded }) {
            Text(if (expanded) "원시값 접기" else "원시값 펼치기 (${cam.info.size}개)")
        }
        if (expanded) {
            cam.info.forEach { KeyValueRow(it.first, it.second) }
        }
    }
}
