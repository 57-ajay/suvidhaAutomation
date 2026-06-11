// api/src/internal/qr.ts
//
// Worker calls this when the UPI QR is on screen. We upload the PNG, write
// aiAgentData.qrCode, and move the lifecycle to qrPaymentNeeded.

import { uploadImage } from "../lib/gcs";
import { config } from "../config";
import { FieldValue } from "../firebase";
import { patchAiAgentData, setAgentStatus } from "./requestDoc";
import { STATUS } from "../lifecycle/statuses";

export interface SaveQRInput {
    requestId: string;
    driverId: string;
    vehicleNumber?: string;
    imageBase64: string;
}

export async function handleSaveQR(input: SaveQRInput) {
    const { requestId, driverId } = input;
    if (!requestId || !driverId) {
        return { ok: false, error: "requestId and driverId required" };
    }

    const dest = `${requestId}_${driverId}/qr_code.png`;
    const up = await uploadImage({
        base64: input.imageBase64,
        destination: dest,
        signedUrlTtlSeconds: config.qrValidityDays * 24 * 60 * 60,
    });

    const expiredAt = new Date(
        Date.now() + config.qrValidityDays * 24 * 60 * 60 * 1000,
    );

    await patchAiAgentData(requestId, driverId, {
        qrCode: {
            url: up.url,
            uploadedAt: FieldValue.serverTimestamp(),
            expiredAt: expiredAt.toISOString(),
            notificationSent: false,
            notificationSentAt: null,
        },
    });

    await setAgentStatus({
        requestId,
        driverId,
        to: STATUS.QR_PAYMENT_NEEDED,
    });

    return { ok: true, url: up.url, expiredAt: expiredAt.toISOString() };
}
