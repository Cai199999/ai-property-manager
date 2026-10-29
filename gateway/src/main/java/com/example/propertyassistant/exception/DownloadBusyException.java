package com.example.propertyassistant.exception;

/**
 * 下载并发数已达到网关上限，快速失败而不是无限排队。
 */
public class DownloadBusyException extends RuntimeException {

    public DownloadBusyException() {
        super("下载并发数已满，请稍后重试");
    }
}
