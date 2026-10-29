package com.example.propertyassistant.dto;

import java.util.Map;

public record GatewayErrorResponse(String requestId, ErrorBody error) {

    public record ErrorBody(
            String code,
            String message,
            Map<String, Object> details
    ) {
        public ErrorBody(String code, String message) {
            this(code, message, Map.of());
        }
    }
}
