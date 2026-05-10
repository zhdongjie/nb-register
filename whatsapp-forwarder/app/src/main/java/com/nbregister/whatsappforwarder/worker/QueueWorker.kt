package com.nbregister.whatsappforwarder.worker

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.nbregister.whatsappforwarder.data.QueueRepository
import com.nbregister.whatsappforwarder.network.OtpWebhookClient
import com.nbregister.whatsappforwarder.settings.SettingsStore

class QueueWorker(
    appContext: Context,
    workerParameters: WorkerParameters,
) : CoroutineWorker(appContext, workerParameters) {
    private val repository = QueueRepository(appContext)
    private val settingsStore = SettingsStore(appContext)
    private val client = OtpWebhookClient()

    override suspend fun doWork(): Result {
        val settings = settingsStore.readAll()
        if (!settings.forwardingEnabled || settings.webhookUrl.isBlank()) {
            return Result.success()
        }

        repository.pruneOldSent()

        var sawRetryableFailure = false
        var batchCount = 0

        while (batchCount < MAX_BATCHES_PER_RUN) {
            val items = repository.getPending(settings.batchSize)
            if (items.isEmpty()) {
                break
            }

            repository.markSending(items)
            for (item in items) {
                val result = client.send(settings.webhookUrl, item)
                if (result.success) {
                    repository.markSent(item)
                } else {
                    repository.markFailure(
                        item = item,
                        maxRetries = settings.maxRetries,
                        error = result.message,
                        permanent = result.permanentFailure,
                    )
                    if (!result.permanentFailure) {
                        sawRetryableFailure = true
                    }
                }
            }
            batchCount += 1
        }

        if (!sawRetryableFailure && repository.hasReadyPending()) {
            WorkerScheduler.enqueueImmediate(applicationContext)
        }

        return if (sawRetryableFailure) Result.retry() else Result.success()
    }

    companion object {
        private const val MAX_BATCHES_PER_RUN = 10
    }
}
