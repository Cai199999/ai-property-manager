package com.example.propertyassistant.config;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.time.Duration;

/**
 * Python Agent 连接配置。
 *
 * 网关只做协议透传，所有路径、超时和容量限制都集中在这里，避免散落在
 * Controller 中形成难以审计的魔法值。
 */
@Validated
@ConfigurationProperties(prefix = "gateway.agent")
public record GatewayProperties(
        @NotBlank String baseUrl,
        @NotBlank String queryPath,
        @NotBlank String codeQueryPath,
        @NotBlank String tablesPath,
        @NotBlank String previewPath,
        @NotBlank String uploadPath,
        @NotBlank String deleteAllConfirmPath,
        @NotBlank String downloadPath,
        @NotNull Duration connectTimeout,
        @NotNull Duration readTimeout,
        @NotNull Duration downloadReadTimeout,
        @Min(1) long maxJsonResponseBytes,
        @Min(1) long downloadMaxBytes,
        @Min(1) int downloadMaxConcurrent,
        @Min(1) long uploadMaxBytes,
        @Min(1) int uploadMaxFiles
) {
}
