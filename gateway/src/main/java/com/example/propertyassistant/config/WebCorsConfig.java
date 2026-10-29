package com.example.propertyassistant.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 浏览器跨域配置。
 *
 * 前端看板运行在 127.0.0.1:8081 / localhost:8081，网关运行在 8080，
 * 二者端口不同，浏览器会先发送 OPTIONS 预检请求。这里只放开助手接口
 * 所需的最小范围，避免使用通配符带来的安全风险。
 */
@Configuration
public class WebCorsConfig implements WebMvcConfigurer {

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
                // 同时兼容 IP 与 localhost 两种本地访问方式。
                .allowedOrigins(
                        "http://127.0.0.1:8081",
                        "http://localhost:8081"
                )
                // 查询/上传使用 POST，表目录和下载使用 GET，预检使用 OPTIONS。
                .allowedMethods("GET", "POST", "OPTIONS")
                // 只允许业务请求所需的请求头。
                .allowedHeaders("Content-Type", "X-Request-Id")
                // 让前端能够读取请求 ID 和下载文件名。
                .exposedHeaders("X-Request-Id", "Content-Disposition", "Content-Length")
                // 预检结果缓存 1 小时，减少重复 OPTIONS 请求。
                .maxAge(3600);
    }
}
