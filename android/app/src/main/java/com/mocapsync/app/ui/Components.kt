package com.mocapsync.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mocapsync.app.core.DeviceProbe

@Composable
fun SectionCard(
    title: String,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(Modifier.padding(14.dp)) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.primary
            )
            Column(Modifier.padding(top = 8.dp)) { content() }
        }
    }
}

/** 키=값 한 줄. 값이 길면 줄바꿈됩니다. */
@Composable
fun KeyValueRow(key: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 2.dp),
        verticalAlignment = Alignment.Top
    ) {
        Text(
            text = key,
            modifier = Modifier.width(150.dp),
            fontSize = 12.sp,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            modifier = Modifier.weight(1f),
            fontSize = 12.sp,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurface
        )
    }
}

fun statusColor(s: DeviceProbe.CheckStatus): Color = when (s) {
    DeviceProbe.CheckStatus.PASS -> StatusColors.pass
    DeviceProbe.CheckStatus.WARN -> StatusColors.warn
    DeviceProbe.CheckStatus.FAIL -> StatusColors.fail
    DeviceProbe.CheckStatus.UNKNOWN -> StatusColors.unknown
}

fun statusLabel(s: DeviceProbe.CheckStatus): String = when (s) {
    DeviceProbe.CheckStatus.PASS -> "통과"
    DeviceProbe.CheckStatus.WARN -> "주의"
    DeviceProbe.CheckStatus.FAIL -> "불가"
    DeviceProbe.CheckStatus.UNKNOWN -> "불명"
}

@Composable
fun CheckRow(check: DeviceProbe.Check) {
    val c = statusColor(check.status)
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.Top
    ) {
        Text(
            text = statusLabel(check.status),
            modifier = Modifier
                .background(c.copy(alpha = 0.18f), RoundedCornerShape(6.dp))
                .padding(horizontal = 8.dp, vertical = 3.dp),
            color = c,
            fontSize = 12.sp,
            fontWeight = FontWeight.Bold
        )
        Column(Modifier.weight(1f)) {
            Text(
                text = check.name,
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface
            )
            Text(
                text = check.detail,
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}
