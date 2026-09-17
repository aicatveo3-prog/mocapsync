package com.mocapsync.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mocapsync.app.BuildConfig
import com.mocapsync.app.core.AppLog
import com.mocapsync.app.core.LogExporter
import kotlinx.coroutines.launch

private const val SCREEN_HOME = "home"
private const val SCREEN_DIAG = "diagnostics"
private const val SCREEN_LOG = "log"
private const val SCREEN_MASTER = "master"
private const val SCREEN_SLAVE = "slave"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MocapApp() {
    var screen by rememberSaveable { mutableStateOf(SCREEN_HOME) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()

    val title = when (screen) {
        SCREEN_DIAG -> "기기 진단"
        SCREEN_LOG -> "로그"
        SCREEN_MASTER -> "마스터"
        SCREEN_SLAVE -> "슬레이브"
        else -> "MocapSync"
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = { Text(title, fontWeight = FontWeight.Bold) },
                navigationIcon = {
                    if (screen != SCREEN_HOME) {
                        TextButton(onClick = { screen = SCREEN_HOME }) { Text("< 뒤로") }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface
                )
            )
        }
    ) { inner ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(inner)
        ) {
            when (screen) {
                SCREEN_DIAG -> DiagnosticsScreen()
                SCREEN_LOG -> LogScreen(
                    onMessage = { msg -> scope.launch { snackbar.showSnackbar(msg) } }
                )
                SCREEN_MASTER -> PlaceholderScreen(
                    role = "마스터",
                    body = "2단계에서 구현합니다.\n\n" +
                        "· NSD(mDNS)로 '_mocapsync._tcp' 서비스를 광고\n" +
                        "· 접속한 슬레이브 목록 표시\n" +
                        "· SNTP 왕복 20~50회 후 오프셋 확정\n" +
                        "· 슬레이브별 동기 오차를 큰 글씨로 표시"
                )
                SCREEN_SLAVE -> PlaceholderScreen(
                    role = "슬레이브",
                    body = "2단계에서 구현합니다.\n\n" +
                        "· NSD로 마스터를 자동 탐색 (IP 입력 없음)\n" +
                        "· 마스터에 접속해 SNTP 응답\n" +
                        "· 자기 오프셋과 왕복지연을 화면에 표시"
                )
                else -> HomeScreen(
                    onOpenDiagnostics = { screen = SCREEN_DIAG },
                    onOpenLog = { screen = SCREEN_LOG },
                    onPickMaster = { screen = SCREEN_MASTER },
                    onPickSlave = { screen = SCREEN_SLAVE },
                    onMessage = { msg -> scope.launch { snackbar.showSnackbar(msg) } }
                )
            }
        }
    }
}

@Composable
private fun HomeScreen(
    onOpenDiagnostics: () -> Unit,
    onOpenLog: () -> Unit,
    onPickMaster: () -> Unit,
    onPickSlave: () -> Unit,
    onMessage: (String) -> Unit
) {
    val context = LocalContext.current
    val logRevision by AppLog.revision.collectAsState()
    val logCount = remember(logRevision) { AppLog.size() }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        SectionCard(title = "이 빌드") {
            KeyValueRow("버전", "${BuildConfig.VERSION_NAME} (code ${BuildConfig.VERSION_CODE})")
            KeyValueRow("git commit", BuildConfig.GIT_SHA)
            KeyValueRow("빌드시각", BuildConfig.BUILD_STAMP)
            KeyValueRow("CI run", BuildConfig.CI_RUN.toString())
            Spacer(Modifier.height(6.dp))
            Text(
                "1단계 목적: CI가 만든 APK가 폰에서 실제로 실행되는지, " +
                    "그리고 이 폰이 60fps · 타임스탬프 · OIS 조건을 만족하는지 확인하는 것입니다. " +
                    "녹화와 동기 기능은 아직 없습니다.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        SectionCard(title = "먼저 여기부터") {
            Text(
                "아래 '기기 진단'을 열고, 화면 아래 '로그 공유'로 결과를 개발자에게 보내주세요. " +
                    "이 리포트가 2·3단계 설계를 결정합니다.",
                fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(Modifier.height(10.dp))
            Button(
                onClick = onOpenDiagnostics,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("기기 진단 열기", fontSize = 16.sp, fontWeight = FontWeight.Bold)
            }
        }

        SectionCard(title = "역할 선택 (2단계 예고)") {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(
                    onClick = onPickMaster,
                    modifier = Modifier.weight(1f)
                ) { Text("마스터") }
                OutlinedButton(
                    onClick = onPickSlave,
                    modifier = Modifier.weight(1f)
                ) { Text("슬레이브") }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "지금은 안내 화면만 나옵니다. 하나의 APK로 두 역할을 모두 수행하는 구조를 미리 잡아둔 것입니다.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        SectionCard(title = "로그") {
            KeyValueRow("버퍼", "$logCount 줄")
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = onOpenLog,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant,
                    contentColor = MaterialTheme.colorScheme.onSurface
                )
            ) { Text("로그 보기") }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = {
                        val err = LogExporter.share(context)
                        if (err != null) onMessage(err)
                    },
                    modifier = Modifier.weight(1f)
                ) { Text("로그 공유") }
                OutlinedButton(
                    onClick = { onMessage(LogExporter.copyToClipboard(context)) },
                    modifier = Modifier.weight(1f)
                ) { Text("복사") }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "공유/복사에는 기기 진단 리포트가 항상 머리말로 붙습니다. " +
                    "그래서 이것만 보내주시면 됩니다.",
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }

        Text(
            "MocapSync · Pose2Sim 기반 마커리스 3D 모션캡쳐",
            fontSize = 11.sp,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(20.dp))
    }
}

@Composable
private fun PlaceholderScreen(role: String, body: String) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp)
    ) {
        SectionCard(title = "$role 역할") {
            Text(
                body,
                fontSize = 14.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}
