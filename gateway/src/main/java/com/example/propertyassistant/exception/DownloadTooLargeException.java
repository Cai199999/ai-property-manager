package com.example.propertyassistant.exception;

/**
 * 上游下载文件超过网关允许的最大字节数。
 */
public class DownloadTooLargeException extends RuntimeException {

    public DownloadTooLargeException(String message) {
        super(message);
    }
}
