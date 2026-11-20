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
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Build
import android.os.IBinder
import android.os.Process
import android.util.Log
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer

/**
 * Servicio en primer plano: escucha en 127.0.0.1:5000 (accesible desde el PC solo vía `adb forward`,
 * es decir, por el cable USB), recibe PCM s16le y lo reproduce con AudioTrack en modo baja latencia.
 *
 * Protocolo: app -> PC: 1 byte 'R' (listo).  PC -> app: cabecera de 6 bytes [rate u32 BE][canales u8][bits u8]
 *            y después PCM crudo.
 */
class AudioStreamService : Service() {

    companion object {
        const val TAG = "UsbAudio"
        const val PORT = 5000
        const val ACTION_STOP = "com.usbaudio.STOP"
        private const val CHANNEL_ID = "usb_audio"
        private const val NOTIF_ID = 1

        const val CHUNK_MS = 10          // tamaño de lectura/escritura
        const val TRACK_BUFFER_MS = 24   // buffer de AudioTrack: súbelo (32-48) si oyes cortes
        const val MAX_BACKLOG_MS = 80    // si se acumula más retraso que esto, se descarta (evita deriva de reloj)

        @Volatile var running = false
    }

    private var worker: Thread? = null
    @Volatile private var server: ServerSocket? = null
    @Volatile private var client: Socket? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { stopSelf(); return START_NOT_STICKY }
        showForeground()
        if (!running) {
            running = true
            worker = Thread({ serve() }, "usb-audio").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        try { client?.close() } catch (_: IOException) {}
        try { server?.close() } catch (_: IOException) {}
        worker?.interrupt()
        super.onDestroy()
    }

    // ───────────── servidor ─────────────
    private fun serve() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        try {
            // Solo loopback: nada accesible desde ninguna red.
            val ss = ServerSocket()
            ss.reuseAddress = true
            ss.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT))
            server = ss
            Log.i(TAG, "Escuchando en 127.0.0.1:$PORT")
            while (running) {
                val c = try { ss.accept() } catch (e: IOException) { break }
                client = c
                try { handle(c) } catch (e: Exception) { Log.w(TAG, "Sesión terminada: $e") }
                finally { try { c.close() } catch (_: IOException) {}; client = null }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error en servidor", e)
        } finally {
            running = false
            try { server?.close() } catch (_: IOException) {}
        }
    }

    private fun handle(c: Socket) {
        c.tcpNoDelay = true
        c.receiveBufferSize = 16 * 1024
        val din = DataInputStream(c.getInputStream())
        c.getOutputStream().apply { write('R'.code); flush() }   // avisa al PC de que estamos listos

        val hdr = ByteArray(6)
        din.readFully(hdr)
        val bb = ByteBuffer.wrap(hdr)
        val rate = bb.int
        val channels = hdr[4].toInt()
        val frameBytes = channels * 2
        Log.i(TAG, "Cliente: $rate Hz, $channels ch")

        val track = createTrack(rate, channels)
        val chunk = frameBytes * rate * CHUNK_MS / 1000
        val maxBacklog = frameBytes * rate * MAX_BACKLOG_MS / 1000
        val buf = ByteArray(chunk)
        updateNotification("Reproduciendo ($rate Hz)")
        try {
            // pre-llenado con silencio para evitar un underrun inicial
            track.write(ByteArray(chunk), 0, chunk)
            track.play()
            while (running) {
                din.readFully(buf)                                   // bloquea hasta tener un bloque completo
                // Si el PC va "adelantado" (buffers TCP/ADB llenos) tiramos lo viejo para recuperar latencia.
                val avail = din.available()
                if (avail > maxBacklog) {
                    var drop = (avail - chunk) / frameBytes * frameBytes
                    Log.w(TAG, "Backlog ${avail}B -> descartando ${drop}B")
                    while (drop > 0) { val k = minOf(drop, chunk); din.readFully(buf, 0, k); drop -= k }
                    din.readFully(buf)
                }
                if (track.write(buf, 0, chunk, AudioTrack.WRITE_BLOCKING) < 0) break
            }
        } finally {
            Log.i(TAG, "Underruns: ${track.underrunCount}")
            track.stop(); track.release()
            updateNotification("Esperando audio del PC por USB…")
        }
    }

    private fun createTrack(rate: Int, channels: Int): AudioTrack {
        val mask = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val frameBytes = channels * 2
        val minBytes = AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
        val am = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val burst = am.getProperty(AudioManager.PROPERTY_OUTPUT_FRAMES_PER_BUFFER)?.toIntOrNull() ?: 256

        var wantFrames = rate * TRACK_BUFFER_MS / 1000
        wantFrames = (wantFrames + burst - 1) / burst * burst            // múltiplo del burst del hardware
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(rate).setChannelMask(mask).build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)   // ruta FAST/AAudio
            .setBufferSizeInBytes(maxOf(minBytes, wantFrames * frameBytes))
            .build()
        track.setBufferSizeInFrames(wantFrames)    // buffer efectivo pequeño dentro de la capacidad
        Log.i(TAG, "AudioTrack burst=$burst frames, buffer=${track.bufferSizeInFrames} frames")
        return track
    }

    // ───────────── notificación (servicio en primer plano) ─────────────
    private fun buildNotification(text: String): Notification {
        val stop = PendingIntent.getService(
            this, 0, Intent(this, AudioStreamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("USB Audio")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_delete, "Detener", stop)
            .build()
    }

    private fun showForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "USB Audio", NotificationManager.IMPORTANCE_LOW))
        val n = buildNotification("Esperando audio del PC por USB…")
        if (Build.VERSION.SDK_INT >= 29)
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        else startForeground(NOTIF_ID, n)
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
}
