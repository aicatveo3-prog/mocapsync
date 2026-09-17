package com.mocapsync.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 촬영 현장은 대체로 어둡고, 폰은 야외에서도 봐야 합니다.
 * 그래서 다크 스킴 하나만 고정합니다 (시스템 설정에 따라 바뀌지 않게).
 */
private val MocapColors = darkColorScheme(
    primary = Color(0xFF4ADE80),
    onPrimary = Color(0xFF04220F),
    primaryContainer = Color(0xFF14532D),
    onPrimaryContainer = Color(0xFFD1FAE5),
    secondary = Color(0xFF60A5FA),
    onSecondary = Color(0xFF04203F),
    background = Color(0xFF101418),
    onBackground = Color(0xFFE6EAEF),
    surface = Color(0xFF171D24),
    onSurface = Color(0xFFE6EAEF),
    surfaceVariant = Color(0xFF222A33),
    onSurfaceVariant = Color(0xFFB6C0CC),
    outline = Color(0xFF3A4552),
    error = Color(0xFFF87171),
    onError = Color(0xFF3B0A0A)
)

/** 판정 색. PASS/WARN/FAIL/UNKNOWN 을 화면에서 한눈에 구분하려고 씁니다. */
object StatusColors {
    val pass = Color(0xFF4ADE80)
    val warn = Color(0xFFFBBF24)
    val fail = Color(0xFFF87171)
    val unknown = Color(0xFF94A3B8)
}

@Composable
fun MocapTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = MocapColors, content = content)
}
