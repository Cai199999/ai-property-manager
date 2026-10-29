package com.example.propertyassistant.exception;

/**
 * 下载文件名不符合白名单规则，可能是路径穿越或非法扩展名。
 */
public class InvalidDownloadNameException extends RuntimeException {

    public InvalidDownloadNameException() {
        super("下载文件名不合法");
    }
}
