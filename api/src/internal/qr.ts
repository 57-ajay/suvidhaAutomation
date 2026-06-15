// Worker calls this the moment the UPI QR is on screen. The PNG upload, the
// aiAgentData.qrCode write, and the qrPaymentNeeded transition happen here so
// the client never observes the status without its image.
//
// notificationSent stays false: an existing Firestore trigger sends the push
// to the driver and flips it.

import { uploadBase64 } from "../lib/gcs";
import { config } from "../config";
import { patchAiAgentData, setAgentStatus, isoIST, ts } from "./requestDoc";
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
    if (!input.imageBase64) {
        return { ok: false, error: "imageBase64 required" };
    }

    const ttlSeconds = config.qrValidityDays * 24 * 60 * 60;
    const up = await uploadBase64({
        base64: input.imageBase64,
        destination: `${requestId}_${driverId}/qr_code.png`,
        signedUrlTtlSeconds: ttlSeconds,
        metadata: input.vehicleNumber
            ? { vehicleNumber: input.vehicleNumber }
            : undefined,
    });

    // const expiredAt = isoIST(new Date(Date.now() + ttlSeconds * 1000));
    const expiresDate = new Date(Date.now() + ttlSeconds * 1000);

    await patchAiAgentData(requestId, driverId, {
        qrCode: {
            url: up.url,
            uploadedAt: ts(),
            expiredAt: ts(expiresDate),
            notificationSent: false,
            notificationSentAt: null,
        },
    });

    await setAgentStatus({ requestId, driverId, to: STATUS.QR_PAYMENT_NEEDED });

    return { ok: true, url: up.url, expiredAt: isoIST(expiresDate) };
}
