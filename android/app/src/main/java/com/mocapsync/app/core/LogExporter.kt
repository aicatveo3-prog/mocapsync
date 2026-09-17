package com.mocapsync.app.core

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.content.FileProvider
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 로그 내보내기.
 *
 * 이 프로젝트에서 이 파일은 "장식"이 아니라 핵심 인프라입니다.
 * 개발자가 실기기를 만질 수 없으므로, 사용자가 폰에서 로그를 꺼내
 * 대화창에 붙여넣는 경로가 유일한 디버깅 채널입니다.
 *
 * 내보내는 내용 = 기기 진단 리포트 + 앱 로그 버퍼 전체.
 * 진단 리포트를 항상 머리말로 붙이는 이유: 로그만 받으면 어떤 기기/어떤 커밋인지
 * 되묻는 왕복이 한 번 더 생기기 때문입니다.
 */
object LogExporter {

    private const val TAG = "LogExporter"
    private const val KEEP_FILES = 8
    private const val CLIPBOARD_LIMIT = 200_000

    fun buildText(context: Context): String = buildString {
        append(DeviceProbe.reportText(context))
        appendLine()
        val lines = AppLog.snapshot()
        appendLine("========================================================")
        appendLine(" 앱 로그 (${lines.size} 줄)")
        appendLine("========================================================")
        if (lines.isEmpty()) {
            appendLine("(비어 있음)")
        } else {
            lines.forEach { appendLine(it) }
        }
    }

    /** cacheDir/logs/ 에 txt 파일을 만들고 File 을 돌려줍니다. */
    fun writeToCache(context: Context): File {
        val dir = File(context.cacheDir, "logs")
        if (!dir.exists()) dir.mkdirs()

        // 오래된 파일 정리 (캐시가 무한정 쌓이지 않게)
        dir.listFiles()
            ?.sortedByDescending { it.lastModified() }
            ?.drop(KEEP_FILES)
            ?.forEach { runCatching { it.delete() } }

        val model = Build.MODEL.replace(Regex("[^A-Za-z0-9]+"), "-").trim('-').ifEmpty { "device" }
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val file = File(dir, "mocapsync_${model}_$stamp.txt")
        file.writeText(buildText(context), Charsets.UTF_8)
        AppLog.i(TAG, "로그 파일 생성: ${file.name} (${file.length()} bytes)")
        return file
    }

    /**
     * 공유 시트를 띄웁니다 (카카오톡 / 드라이브 / 메일 / 파일로 저장 등).
     * @return 실패 시 사용자에게 보여줄 메시지, 성공 시 null
     */
    fun share(context: Context): String? {
        return try {
            val file = writeToCache(context)
            val uri = FileProvider.getUriForFile(
                context,
                "${context.packageName}.fileprovider",
                file
            )
            val send = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_STREAM, uri)
                putExtra(Intent.EXTRA_SUBJECT, "MocapSync 로그 ${file.name}")
                // 일부 앱은 첨부보다 본문을 먼저 읽으므로 안내문을 넣어둡니다
                putExtra(Intent.EXTRA_TEXT, "MocapSync 로그 파일입니다. (${file.name})")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            val chooser = Intent.createChooser(send, "로그 내보내기")
            chooser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(chooser)
            null
        } catch (t: Throwable) {
            AppLog.e(TAG, "공유 실패", t)
            "공유 실패: ${t.javaClass.simpleName}: ${t.message}"
        }
    }

    /** 붙여넣기용. 로그가 너무 길면 뒷부분(최신)만 잘라서 복사합니다. */
    fun copyToClipboard(context: Context): String {
        val full = buildText(context)
        val text = if (full.length <= CLIPBOARD_LIMIT) {
            full
        } else {
            "(앞부분 ${full.length - CLIPBOARD_LIMIT} 글자 생략 — 전체는 '공유'로 파일을 받으세요)\n" +
                full.takeLast(CLIPBOARD_LIMIT)
        }
        return try {
            val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            cm.setPrimaryClip(ClipData.newPlainText("MocapSync 로그", text))
            AppLog.i(TAG, "클립보드 복사 완료 (${text.length} 글자)")
            "클립보드에 복사했습니다 (${text.length} 글자)"
        } catch (t: Throwable) {
            AppLog.e(TAG, "클립보드 복사 실패", t)
            "클립보드 복사 실패: ${t.message}"
        }
    }
}
