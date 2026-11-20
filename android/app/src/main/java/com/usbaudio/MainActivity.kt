package com.usbaudio

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/** UI mínima: un botón Iniciar/Detener. El trabajo real lo hace AudioStreamService. */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var button: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Android 13+: permiso para mostrar la notificación del servicio en primer plano
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)

        status = TextView(this).apply { textSize = 18f; gravity = Gravity.CENTER }
        button = Button(this).apply { setOnClickListener { toggle() } }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(48, 48, 48, 48)
            addView(status)
            addView(button)
        })

        if (intent.getBooleanExtra("autostart", false)) startSvc()  // lanzado por el script del PC
        refresh()
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

    private fun startSvc() {
        if (!AudioStreamService.running)
            startForegroundService(Intent(this, AudioStreamService::class.java))
    }

    private fun refresh() {
        // el servicio tarda unos ms en marcar 'running'; refrescamos dos veces
        val update = {
            val on = AudioStreamService.running
            status.text = if (on) "Escuchando en 127.0.0.1:${AudioStreamService.PORT}\n(túnel USB)" else "Detenido"
            button.text = if (on) "Detener" else "Iniciar"
        }
        update()
        button.postDelayed({ update() }, 400)
    }
}
