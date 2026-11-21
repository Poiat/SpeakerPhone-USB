package com.usbaudio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.Process
import android.util.Log
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer

class AudioStreamService : Service() {

    companion object {
        const val TAG = "UsbAudio"
        const val PORT = 5000
        const val ACTION_STOP = "com.usbaudio.STOP"
        const val ACTION_STATUS = "com.usbaudio.STATUS"
        const val EXTRA_BUFFER_MS = "buffer_ms"
        private const val CHANNEL_ID = "usb_audio"
        private const val NOTIF_ID = 1

        const val PROTOCOL_VERSION = 1
        const val HEADER_SIZE = 11
        const val TRACK_BUFFER_MS = 36  // Perfil "Equilibrado" por defecto
        const val MAX_BACKLOG_MS = 80

        @Volatile var running = false
        @Volatile var streaming = false
        @Volatile var activeFormat: String? = null
        @Volatile var currentBufferMs: Int = TRACK_BUFFER_MS
        @Volatile var lastUnderruns: Int = 0
    }

    private var worker: Thread? = null
    @Volatile private var server: ServerSocket? = null
    @Volatile private var client: Socket? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var trackBufferMs: Int = TRACK_BUFFER_MS
    @Volatile private var audioFocusRequest: AudioFocusRequest? = null
    private var deviceCallback: AudioDeviceCallback? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private val audioManager: AudioManager by lazy {
        getSystemService(Context.AUDIO_SERVICE) as AudioManager
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        val reqBuf = intent?.getIntExtra(EXTRA_BUFFER_MS, trackBufferMs) ?: trackBufferMs
        val newBuf = reqBuf.coerceIn(12, 120)
        val bufferChanged = newBuf != trackBufferMs
        trackBufferMs = newBuf
        currentBufferMs = newBuf

        showForeground()
        acquireWakeLock()
        requestAudioFocus()
        registerAudioDeviceCallback()
        if (!running) {
            running = true
            worker = Thread({ serve() }, "usb-audio").also { it.start() }
        } else if (bufferChanged && streaming) {
            try { client?.close() } catch (_: IOException) {}
        }
        broadcastStatus()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        streaming = false
        activeFormat = null
        try { client?.close() } catch (_: IOException) {}
        try { server?.close() } catch (_: IOException) {}
        worker?.interrupt()
        releaseWakeLock()
        abandonAudioFocus()
        unregisterAudioDeviceCallback()
        broadcastStatus()
        super.onDestroy()
    }

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$TAG:AudioWakeLock").apply {
                setReferenceCounted(false)
            }
        }
        if (wakeLock?.isHeld == false) {
            // Servicio en primer plano: mantener la CPU despierta hasta onDestroy.
            try { wakeLock?.acquire() } catch (_: Exception) {}
        }
    }

    private fun releaseWakeLock() {
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (_: Exception) {}
    }

    private fun requestAudioFocus(): Boolean {
        if (audioFocusRequest != null) return true
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
            .build()
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
            .setAudioAttributes(attributes)
            .setOnAudioFocusChangeListener { change ->
                when (change) {
                    AudioManager.AUDIOFOCUS_LOSS,
                    AudioManager.AUDIOFOCUS_LOSS_TRANSIENT,
                    AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> {
                        audioFocusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
                        audioFocusRequest = null
                        try { client?.close() } catch (_: IOException) {}
                        updateNotification("Audio pausado: otra aplicación usa la salida")
                        broadcastStatus()
                    }
                    AudioManager.AUDIOFOCUS_GAIN -> {
                        updateNotification("Esperando audio del PC (${trackBufferMs} ms)…")
                        broadcastStatus()
                    }
                }
            }
            .build()
        val granted = audioManager.requestAudioFocus(request) ==
            AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        if (granted) {
            audioFocusRequest = request
            return true
        }
        Log.w(TAG, "No se concedió el audio focus")
        return false
    }

    private fun abandonAudioFocus() {
        audioFocusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        audioFocusRequest = null
    }

    private fun registerAudioDeviceCallback() {
        if (deviceCallback != null) return
        val callback = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>?) {
                if (addedDevices?.any { it.isSink } == true) restartForRouteChange()
            }

            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>?) {
                if (removedDevices?.any { it.isSink } == true) restartForRouteChange()
            }
        }
        deviceCallback = callback
        audioManager.registerAudioDeviceCallback(callback, mainHandler)
    }

    private fun unregisterAudioDeviceCallback() {
        deviceCallback?.let { audioManager.unregisterAudioDeviceCallback(it) }
        deviceCallback = null
    }

    private fun restartForRouteChange() {
        if (!streaming) return
        try { client?.close() } catch (_: IOException) {}
        updateNotification("Salida de audio cambiada; reconectando…")
        broadcastStatus()
    }

    private fun broadcastStatus() {
        sendBroadcast(Intent(ACTION_STATUS).setPackage(packageName))
    }

    private fun serve() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        try {
            val ss = ServerSocket()
            ss.reuseAddress = true
            ss.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT))
            server = ss
            while (running) {
                val c = try { ss.accept() } catch (_: IOException) { break }
                client = c
                try { handle(c) } catch (e: Exception) { Log.w(TAG, "Sesión terminada: $e") }
                finally {
                    streaming = false
                    activeFormat = null
                    broadcastStatus()
                    try { c.close() } catch (_: IOException) {}
                    client = null
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error en servidor", e)
        } finally {
            running = false
            streaming = false
            activeFormat = null
            try { server?.close() } catch (_: IOException) {}
            broadcastStatus()
        }
    }

    private fun handle(c: Socket) {
        if (!requestAudioFocus()) {
            Log.w(TAG, "Sesión rechazada: sin audio focus")
            return
        }
        c.tcpNoDelay = true
        c.soTimeout = 3000
        c.receiveBufferSize = 16 * 1024
        val din = DataInputStream(c.getInputStream())
        c.getOutputStream().apply { write(82); flush() }

        val hdr = ByteArray(HEADER_SIZE)
        din.readFully(hdr)
        require(hdr[0] == 'U'.code.toByte() && hdr[1] == 'A'.code.toByte()) {
            "Protocolo no compatible (magic invalido)"
        }
        val protocolVersion = hdr[2].toInt() and 0xFF
        require(protocolVersion == PROTOCOL_VERSION) {
            "Version de protocolo no soportada: $protocolVersion"
        }
        val bb = ByteBuffer.wrap(hdr, 3, HEADER_SIZE - 3)
        val rate = bb.int
        val channels = bb.get().toInt() and 0xFF
        val bits = bb.get().toInt() and 0xFF
        val chunkMs = bb.short.toInt() and 0xFFFF
        require(rate in 8000..192000) { "Rate invalido: $rate" }
        require(channels == 1 || channels == 2) { "Canales invalidos: $channels" }
        require(bits == 16) { "Bits invalidos: $bits" }
        require(chunkMs in 2..100) { "Chunk invalido: $chunkMs" }

        val frameBytes = channels * 2
        val fmtLabel = "$rate Hz · ${if (channels == 1) "Mono" else "Estéreo"}"
        streaming = true
        activeFormat = fmtLabel
        broadcastStatus()

        val track = createTrack(rate, channels)
        val chunk = frameBytes * rate * chunkMs / 1000
        val backlogMs = maxOf(MAX_BACKLOG_MS, chunkMs * 2)
        val maxBacklog = frameBytes * rate * backlogMs / 1000
        val buf = ByteArray(chunk)
        updateNotification("Reproduciendo · $fmtLabel (${trackBufferMs} ms)")
        try {
            track.write(ByteArray(chunk), 0, chunk)
            track.play()
            while (running) {
                try {
                    din.readFully(buf)
                } catch (_: SocketTimeoutException) {
                    continue
                }
                val avail = din.available()
                if (avail > maxBacklog) {
                    var drop = (avail - chunk) / frameBytes * frameBytes
                    while (drop > 0) {
                        val k = minOf(drop, chunk)
                        try { din.readFully(buf, 0, k) } catch (_: SocketTimeoutException) { break }
                        drop -= k
                    }
                    try { din.readFully(buf) } catch (_: SocketTimeoutException) { continue }
                }
                if (track.write(buf, 0, chunk, AudioTrack.WRITE_BLOCKING) < 0) break
            }
        } finally {
            lastUnderruns = track.underrunCount
            streaming = false
            activeFormat = null
            broadcastStatus()
            track.stop()
            track.release()
            updateNotification("Esperando audio del PC (${trackBufferMs} ms)…")
        }
    }

    private fun createTrack(rate: Int, channels: Int): AudioTrack {
        val mask = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val frameBytes = channels * 2
        val minBytes = AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val burst = am.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER)?.toIntOrNull() ?: 256

        var wantFrames = rate * trackBufferMs / 1000
        wantFrames = ((wantFrames + burst - 1) / burst * burst).coerceAtLeast(burst)
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(rate).setChannelMask(mask).build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            .setBufferSizeInBytes(maxOf(minBytes, wantFrames * frameBytes))
            .build()
        track.setBufferSizeInFrames(wantFrames)
        return track
    }

    private fun buildNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val stopIntent = PendingIntent.getService(
            this, 1, Intent(this, AudioStreamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        val stopAction = Notification.Action.Builder(
            android.R.drawable.ic_delete, "Detener", stopIntent
        ).build()

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("SpeakerPhone USB · ${MainActivity.APP_VERSION}")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(openApp)
            .setOngoing(true)
            .addAction(stopAction)
            .build()
    }

    private fun showForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "SpeakerPhone USB", NotificationManager.IMPORTANCE_LOW)
        )
        val n = buildNotification("Esperando audio del PC (${trackBufferMs} ms)…")
        if (Build.VERSION.SDK_INT >= 29)
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        else startForeground(NOTIF_ID, n)
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
}
