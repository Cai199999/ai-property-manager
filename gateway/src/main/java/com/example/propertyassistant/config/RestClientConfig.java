package com.example.propertyassistant.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import java.net.http.HttpClient;

@Configuration
@EnableConfigurationProperties(GatewayProperties.class)
public class RestClientConfig {

    /**
     * 使用 JDK HttpClient，显式设置连接与读取超时，避免 Agent 卡死拖垮网关线程池。
     */
    @Bean
    public RestClient agentRestClient(
            RestClient.Builder restClientBuilder,
            GatewayProperties properties
    ) {
        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(properties.connectTimeout())
                .version(HttpClient.Version.HTTP_1_1)
                .build();

        JdkClientHttpRequestFactory requestFactory = new JdkClientHttpRequestFactory(httpClient);
        requestFactory.setReadTimeout(properties.readTimeout());

        // 复用 Boot 配置过的 Builder，确保 spring.jackson 的 SNAKE_CASE 对出入参都生效。
        return restClientBuilder.clone()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory)
                .build();
    }
}
