package com.example.propertyassistant.exception;

/**
 * Python Agent 返回非 2xx，或网关无法完成调用。
 */
public class UpstreamAgentException extends RuntimeException {

    private final int upstreamStatus;
    private final String requestId;

    public UpstreamAgentException(int upstreamStatus, String message, String requestId) {
        super(message);
        this.upstreamStatus = upstreamStatus;
        this.requestId = requestId;
    }

    public int getUpstreamStatus() {
        return upstreamStatus;
    }

    public String getRequestId() {
        return requestId;
    }
}
