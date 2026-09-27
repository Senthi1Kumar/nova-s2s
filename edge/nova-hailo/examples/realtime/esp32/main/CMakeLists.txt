idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_i2s esp_driver_gpio nvs_flash esp_netif esp_event json mbedtls protocol_examples_common
    EMBED_TXTFILES "nova_cert.pem")
