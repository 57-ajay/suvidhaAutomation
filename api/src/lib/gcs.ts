// Upload a base64 blob under the configured prefix and return a signed URL.
// Signed URLs require the service-account key (IAM signBlob is not used), which
// is why the key is mounted into the API container.

import { bucket } from "../firebase";
import { config } from "../config";

export interface UploadInput {
    base64: string;
    destination: string; // relative to the prefix
    contentType?: string;
    signedUrlTtlSeconds: number;
    metadata?: Record<string, string>;
    prefix?: string; // overrides config.gcsPrefix (per-task namespacing)
}

export interface UploadResult {
    url: string;
    gcsPath: string;
    bytes: number;
}

export async function uploadBase64(input: UploadInput): Promise<UploadResult> {
    // const gcsPath = `${config.gcsPrefix}/${input.destination}`;
    const gcsPath = `${input.prefix ?? config.gcsPrefix}/${input.destination}`;
    const buffer = Buffer.from(input.base64, "base64");
    if (buffer.length === 0) throw new Error("upload payload is empty");

    const file = bucket.file(gcsPath);
    await file.save(buffer, {
        metadata: {
            contentType: input.contentType ?? "image/png",
            ...(input.metadata ? { metadata: input.metadata } : {}),
        },
    });

    const [url] = await file.getSignedUrl({
        action: "read",
        expires: Date.now() + input.signedUrlTtlSeconds * 1000,
    });

    return { url, gcsPath, bytes: buffer.length };
}
