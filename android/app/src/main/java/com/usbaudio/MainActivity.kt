package com.usbaudio

import android.Manifest
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class MainActivity : Activity() {
    companion object {
        const val APP_VERSION = "v4.0.11"
        private const val PREFS_NAME = "usbaudio_prefs"
        private const val KEY_BUFFER_MS = "buffer_ms"
        private val BUFFER_OPTIONS = listOf(24, 36, 48)
    }

    private lateinit var dotView: View
    private lateinit var badgeText: TextView
    private lateinit var statusTitle: TextView
    private lateinit var statusSub: TextView
    private lateinit var deviceValue: TextView
    private lateinit var deviceSub: TextView
    private lateinit var formatValue: TextView
    private lateinit var underrunsValue: TextView
    private lateinit var bufferInfo: TextView
    private lateinit var actionButton: Button
    private val bufferButtons = mutableMapOf<Int, Button>()
    private val uiHandler = Handler(Looper.getMainLooper())
    private var selectedBufferMs: Int = AudioStreamService.TRACK_BUFFER_MS

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) { refresh() }
    }

    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) { refresh() }
        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) { refresh() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        selectedBufferMs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getInt(KEY_BUFFER_MS, AudioStreamService.TRACK_BUFFER_MS)
            .let { if (it in BUFFER_OPTIONS) it else AudioStreamService.TRACK_BUFFER_MS }

        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
        window.statusBarColor = Color.parseColor("#0B0F17")
        window.navigationBarColor = Color.parseColor("#0B0F17")

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            setBackgroundColor(Color.parseColor("#0B0F17"))
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#0B0F17"))
            setPadding(dp(22), dp(28), dp(22), dp(24))
        }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, 0, 0, dp(18))
        }
        val titleCol = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        titleCol.addView(TextView(this).apply {
            text = "SpeakerPhone USB"
            textSize = 22f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#F8FAFC"))
        })
        titleCol.addView(TextView(this).apply {
            text = "Receptor de audio PCM de baja latencia"
            textSize = 13f
            setTextColor(Color.parseColor("#94A3B8"))
            setPadding(0, dp(2), 0, 0)
        })
        val versionPill = TextView(this).apply {
            text = APP_VERSION
            textSize = 12f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#38BDF8"))
            setPadding(dp(12), dp(6), dp(12), dp(6))
            background = roundRect("#162336", "#1E3A5F", 20)
        }
        header.addView(titleCol)
        header.addView(versionPill)
        root.addView(header)

        val statusCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(16), dp(18), dp(16))
            background = roundRect("#111827", "#1F2937", 18)
        }
        val pillRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, 0, 0, dp(10))
        }
        dotView = View(this).apply {
            layoutParams = LinearLayout.LayoutParams(dp(10), dp(10)).apply { marginEnd = dp(8) }
            background = circle("#64748B")
        }
        badgeText = TextView(this).apply {
            textSize = 12f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#94A3B8"))
        }
        pillRow.addView(dotView)
        pillRow.addView(badgeText)
        statusTitle = TextView(this).apply {
            textSize = 19f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#F1F5F9"))
        }
        statusSub = TextView(this).apply {
            textSize = 13f
            setTextColor(Color.parseColor("#94A3B8"))
            setPadding(0, dp(4), 0, 0)
        }
        statusCard.addView(pillRow)
        statusCard.addView(statusTitle)
        statusCard.addView(statusSub)
        root.addView(statusCard)

        val deviceCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(14), dp(18), dp(14))
            background = roundRect("#111827", "#1F2937", 16)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(12) }
        }
        deviceCard.addView(TextView(this).apply {
            text = "DISPOSITIVO CONECTADO Y SALIDA"
            textSize = 10f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#64748B"))
        })
        deviceValue = TextView(this).apply {
            textSize = 15f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#38BDF8"))
            setPadding(0, dp(5), 0, 0)
        }
        deviceSub = TextView(this).apply {
            textSize = 12f
            setTextColor(Color.parseColor("#94A3B8"))
            setPadding(0, dp(3), 0, 0)
        }
        deviceCard.addView(deviceValue)
        deviceCard.addView(deviceSub)
        root.addView(deviceCard)

        val metricsRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(12) }
        }
        val formatCard = makeMetricCard("FORMATO ACTIVO")
        formatValue = formatCard.second
        formatCard.first.layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = dp(6) }
        val underrunCard = makeMetricCard("UNDERRUNS")
        underrunsValue = underrunCard.second
        underrunCard.first.layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginStart = dp(6) }
        metricsRow.addView(formatCard.first)
        metricsRow.addView(underrunCard.first)
        root.addView(metricsRow)

        val bufferCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(14), dp(18), dp(14))
            background = roundRect("#111827", "#1F2937", 16)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(12) }
        }
        bufferCard.addView(TextView(this).apply {
            text = "BÚFER DE AUDIO (LATENCIA VS ESTABILIDAD)"
            textSize = 11f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#94A3B8"))
        })
        val bufRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(10) }
        }
        BUFFER_OPTIONS.forEachIndexed { idx, ms ->
            val btn = Button(this).apply {
                text = "$ms ms"
                textSize = 13f
                isAllCaps = false
                setTypeface(null, Typeface.BOLD)
                setPadding(dp(8), dp(10), dp(8), dp(10))
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                    if (idx > 0) marginStart = dp(8)
                }
                setOnClickListener { selectBuffer(ms) }
            }
            bufferButtons[ms] = btn
            bufRow.addView(btn)
        }
        bufferCard.addView(bufRow)
        bufferInfo = TextView(this).apply {
            textSize = 12f
            setTextColor(Color.parseColor("#64748B"))
            setPadding(0, dp(8), 0, 0)
        }
        bufferCard.addView(bufferInfo)
        root.addView(bufferCard)

        root.addView(View(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(18), 1f)
        })
        actionButton = Button(this).apply {
            textSize = 16f
            isAllCaps = false
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.WHITE)
            setPadding(dp(20), dp(16), dp(20), dp(16))
            setOnClickListener { toggle() }
        }
        root.addView(actionButton)

        root.addView(TextView(this).apply {
            text = "127.0.0.1:${AudioStreamService.PORT} · WakeLock activo · $APP_VERSION"
            textSize = 11f
            gravity = Gravity.CENTER
            setTextColor(Color.parseColor("#475569"))
            setPadding(0, dp(12), 0, 0)
        })

        scroll.addView(root)
        setContentView(scroll)
        updateBufferButtons()
        if (intent.getBooleanExtra("autostart", false)) startSvc()
        refresh()
    }

    private fun detectAudioOutput(): String {
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val outputs = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
        val priority = listOf(
            AudioDeviceInfo.TYPE_BLE_HEADSET,
            AudioDeviceInfo.TYPE_BLE_SPEAKER,
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_USB_HEADSET,
            AudioDeviceInfo.TYPE_USB_DEVICE,
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
            AudioDeviceInfo.TYPE_WIRED_HEADSET,
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
        )
        val best = priority.firstNotNullOfOrNull { type -> outputs.firstOrNull { it.type == type } } ?: outputs.firstOrNull()
        return when (best?.type) {
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
            AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_BLE_SPEAKER ->
                "Bluetooth · ${best.productName?.toString()?.takeIf { it.isNotBlank() } ?: "Dispositivo BT"}"
            AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE ->
                "USB Audio · ${best.productName?.toString()?.takeIf { it.isNotBlank() } ?: "DAC USB"}"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET ->
                "Audífonos con cable"
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "Altavoz (${Build.MODEL})"
            else -> "Salida (${Build.MODEL})"
        }
    }

    private fun selectBuffer(ms: Int) {
        selectedBufferMs = ms
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit().putInt(KEY_BUFFER_MS, ms).apply()
        updateBufferButtons()
        if (AudioStreamService.running) startSvc(forceUpdate = true)
        refresh()
    }

    private fun updateBufferButtons() {
        BUFFER_OPTIONS.forEach { ms ->
            val active = ms == selectedBufferMs
            bufferButtons[ms]?.apply {
                background = if (active) roundRect("#1D4ED8", "#3B82F6", 12) else roundRect("#0F172A", "#1E293B", 12)
                setTextColor(if (active) Color.WHITE else Color.parseColor("#94A3B8"))
            }
        }
        bufferInfo.text = when (selectedBufferMs) {
            24 -> "24 ms · Mínima latencia (juegos)"
            36 -> "36 ms · Equilibrio latencia/estabilidad"
            else -> "48 ms · Mayor margen contra cortes USB"
        }
    }

    private fun makeMetricCard(label: String): Pair<LinearLayout, TextView> {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
            background = roundRect("#111827", "#1F2937", 16)
        }
        card.addView(TextView(this).apply {
            text = label
            textSize = 10f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#64748B"))
        })
        val valView = TextView(this).apply {
            text = "—"
            textSize = 15f
            setTypeface(null, Typeface.BOLD)
            setTextColor(Color.parseColor("#E2E8F0"))
            setPadding(0, dp(6), 0, 0)
        }
        card.addView(valView)
        return Pair(card, valView)
    }

    private fun roundRect(fillHex: String, strokeHex: String, radiusDp: Int): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(radiusDp).toFloat()
            setColor(Color.parseColor(fillHex))
            setStroke(dp(1), Color.parseColor(strokeHex))
        }

    private fun circle(fillHex: String): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.parseColor(fillHex))
        }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(AudioStreamService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        else { @Suppress("DEPRECATION") registerReceiver(statusReceiver, filter) }
        (getSystemService(Context.AUDIO_SERVICE) as AudioManager).registerAudioDeviceCallback(deviceCallback, uiHandler)
    }

    override fun onStop() {
        try { unregisterReceiver(statusReceiver) } catch (_: Exception) {}
        (getSystemService(Context.AUDIO_SERVICE) as AudioManager).unregisterAudioDeviceCallback(deviceCallback)
        super.onStop()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra("autostart", false)) startSvc()
        refresh()
    }

    override fun onResume() { super.onResume(); refresh() }

    private fun toggle() {
        if (AudioStreamService.running) stopService(Intent(this, AudioStreamService::class.java))
        else startSvc()
        refresh()
    }

    private fun startSvc(forceUpdate: Boolean = false) {
        if (!AudioStreamService.running || forceUpdate) {
            startForegroundService(Intent(this, AudioStreamService::class.java).apply {
                putExtra(AudioStreamService.EXTRA_BUFFER_MS, selectedBufferMs)
            })
        }
    }

    private fun refresh() {
        val update = {
            val on = AudioStreamService.running
            val active = AudioStreamService.streaming
            val fmt = AudioStreamService.activeFormat
            val underruns = AudioStreamService.lastUnderruns
            val activeBuf = if (on) AudioStreamService.currentBufferMs else selectedBufferMs

            deviceValue.text = detectAudioOutput()
            deviceSub.text = when {
                active -> "Origen: PC conectado por túnel USB ADB (:5000)"
                on -> "Origen: En espera de túnel USB ADB (:5000)"
                else -> "Origen: Servicio desconectado"
            }
            when {
                !on -> {
                    dotView.background = circle("#64748B")
                    badgeText.text = "INACTIVO"
                    badgeText.setTextColor(Color.parseColor("#94A3B8"))
                    statusTitle.text = "Servicio detenido"
                    statusSub.text = "Pulsa Iniciar para abrir el puerto USB ${AudioStreamService.PORT}"
                    formatValue.text = "Sin señal"
                    actionButton.text = "Iniciar receptor USB"
                    actionButton.background = roundRect("#16A34A", "#22C55E", 14)
                }
                active && fmt != null -> {
                    dotView.background = circle("#22C55E")
                    badgeText.text = "EN VIVO · CONECTADO"
                    badgeText.setTextColor(Color.parseColor("#4ADE80"))
                    statusTitle.text = "Transmitiendo audio"
                    statusSub.text = "Búfer activo: ${activeBuf} ms por cable USB"
                    formatValue.text = fmt
                    actionButton.text = "Detener receptor"
                    actionButton.background = roundRect("#DC2626", "#EF4444", 14)
                }
                else -> {
                    dotView.background = circle("#F59E0B")
                    badgeText.text = "ESPERANDO PC"
                    badgeText.setTextColor(Color.parseColor("#FBBF24"))
                    statusTitle.text = "Listo en 127.0.0.1:${AudioStreamService.PORT}"
                    statusSub.text = "Búfer configurado: ${activeBuf} ms · Esperando conexión USB"
                    formatValue.text = "En espera"
                    actionButton.text = "Detener receptor"
                    actionButton.background = roundRect("#DC2626", "#EF4444", 14)
                }
            }
            underrunsValue.text = underruns.toString()
        }
        update()
        uiHandler.postDelayed({ update() }, 350)
    }
}
