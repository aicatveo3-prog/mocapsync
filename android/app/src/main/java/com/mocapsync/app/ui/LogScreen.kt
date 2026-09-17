package com.mocapsync.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mocapsync.app.core.AppLog
import com.mocapsync.app.core.LogExporter

@Composable
fun LogScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val revision by AppLog.revision.collectAsState()
    val lines = remember(revision) { AppLog.snapshot() }
    val listState = rememberLazyListState()

    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) {
            listState.scrollToItem(lines.size - 1)
        }
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Button(
                onClick = {
                    val err = LogExporter.share(context)
                    if (err != null) onMessage(err)
                },
                modifier = Modifier.weight(1f)
            ) { Text("공유", fontSize = 13.sp) }
            OutlinedButton(
                onClick = { onMessage(LogExporter.copyToClipboard(context)) },
                modifier = Modifier.weight(1f)
            ) { Text("복사", fontSize = 13.sp) }
            OutlinedButton(
                onClick = {
                    AppLog.clear()
                    AppLog.i("Log", "로그 버퍼를 비웠습니다")
                    onMessage("로그를 비웠습니다")
                },
                modifier = Modifier.weight(1f)
            ) { Text("지우기", fontSize = 13.sp) }
        }

        Text(
            "${lines.size} 줄 · 최대 4000줄까지 보관 (오래된 줄부터 버립니다)",
            fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 2.dp)
        )

        LazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 10.dp)
        ) {
            items(lines) { line ->
                Text(
                    text = line,
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                    color = when {
                        line.contains(" E/") -> MaterialTheme.colorScheme.error
                        line.contains(" W/") -> StatusColors.warn
                        line.contains(" M/") -> MaterialTheme.colorScheme.primary
                        else -> MaterialTheme.colorScheme.onSurface
                    },
                    modifier = Modifier.padding(vertical = 1.dp)
                )
            }
        }
    }
}
