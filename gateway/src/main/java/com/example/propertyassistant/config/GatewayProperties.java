package com.example.propertyassistant.config;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.time.Duration;

/**
 * Python Agent 连接配置。
 */
@Validated
@ConfigurationProperties(prefix = "gateway.agent")
public record GatewayProperties(
        @NotBlank String baseUrl,
        @NotBlank String queryPath,
        @NotNull Duration connectTimeout,
        @NotNull Duration readTimeout
) {
}
