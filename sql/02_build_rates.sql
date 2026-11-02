-- Build two price tables from raw services, preserving source positions and decimal amounts.
-- IN: service -> rate group -> price. OON: service -> allowed group -> payment -> provider child.
USE ROLE SYSADMIN;
USE WAREHOUSE UHC_WH;
USE DATABASE UHC_DB;
USE SCHEMA ANALYTICS;

-- The download ledger supplies the original filename; it must contain one row per URL.
CREATE OR REPLACE VIEW V_RAW_PRICING AS
SELECT SHA2(CAST(r.PAYLOAD:source_url AS VARCHAR),256) AS FILE_KEY,
       CAST(r.PAYLOAD:source_url AS VARCHAR) AS SOURCE_URL,
       CAST(d.PAYLOAD:original_filename AS VARCHAR) AS SOURCE_FILENAME,
       CAST(r.PAYLOAD:section AS VARCHAR) AS SECTION,
       CAST(r.PAYLOAD:source_position AS INTEGER) AS SOURCE_POSITION,
       r.PAYLOAD:document AS DOCUMENT, r.PAYLOAD:section_counts AS SECTION_COUNTS,
       CAST(r.PAYLOAD:file_type AS VARCHAR) AS FILE_TYPE
FROM RAW_PRICING_INPUT r
JOIN RAW_DOWNLOAD_INPUT d ON r.PAYLOAD:source_url = d.PAYLOAD:source_url;

CREATE OR REPLACE TABLE IN_NETWORK_RATES_SQL AS
WITH rate_groups AS (
SELECT raw_service.FILE_KEY, raw_service.SOURCE_URL, raw_service.SOURCE_FILENAME, raw_service.SOURCE_POSITION AS SERVICE_POSITION, raw_service.DOCUMENT AS SERVICE,
       CAST(group_item.INDEX AS INTEGER) AS RATE_GROUP_POSITION, group_item.VALUE AS RATE_GROUP,
       CONCAT(raw_service.FILE_KEY, ':', CAST(raw_service.SOURCE_POSITION AS VARCHAR), ':', CAST(group_item.INDEX AS VARCHAR)) AS RATE_GROUP_KEY
FROM V_RAW_PRICING raw_service, LATERAL FLATTEN(INPUT => raw_service.DOCUMENT:negotiated_rates) group_item
WHERE raw_service.SECTION = 'in_network'
)
SELECT rate_group.FILE_KEY, rate_group.SOURCE_URL, rate_group.SOURCE_FILENAME, rate_group.SERVICE_POSITION,
       UHC_TEXT(rate_group.SERVICE:name) AS NAME, UHC_TEXT(rate_group.SERVICE:description) AS DESCRIPTION,
       UHC_TEXT(rate_group.SERVICE:billing_code) AS BILLING_CODE,
       UHC_TEXT(rate_group.SERVICE:billing_code_type) AS BILLING_CODE_TYPE,
       UHC_TEXT(rate_group.SERVICE:billing_code_type_version) AS BILLING_CODE_TYPE_VERSION,
       CONCAT(rate_group.RATE_GROUP_KEY, ':', CAST(price_item.INDEX AS VARCHAR)) AS RATE_KEY, rate_group.RATE_GROUP_KEY,
       rate_group.RATE_GROUP_POSITION, CAST(price_item.INDEX AS INTEGER) AS PRICE_POSITION,
       UHC_TEXT(rate_group.SERVICE:negotiation_arrangement) AS NEGOTIATION_ARRANGEMENT,
       UHC_TEXT(rate_group.SERVICE:severity_of_illness) AS SEVERITY_OF_ILLNESS,
       CAST(UHC_AMOUNT(price_item.VALUE:negotiated_rate) AS NUMBER(18,6)) AS NEGOTIATED_RATE,
       UHC_TEXT(price_item.VALUE:negotiated_type) AS NEGOTIATED_TYPE,
       UHC_TEXT(price_item.VALUE:billing_class) AS BILLING_CLASS, UHC_TEXT(price_item.VALUE:setting) AS SETTING,
       AS_ARRAY(price_item.VALUE:service_code) AS SERVICE_CODE,
       AS_ARRAY(price_item.VALUE:billing_code_modifier) AS BILLING_CODE_MODIFIER,
       TO_DATE(NULLIF(UHC_TEXT(price_item.VALUE:expiration_date),'')) AS EXPIRATION_DATE,
       UHC_TEXT(price_item.VALUE:additional_information) AS ADDITIONAL_INFORMATION
FROM rate_groups rate_group, LATERAL FLATTEN(INPUT => rate_group.RATE_GROUP:negotiated_prices) price_item;

-- Allowed amount belongs to the payment; billed charge belongs to its provider child.
CREATE OR REPLACE TABLE OUT_OF_NETWORK_RATES_SQL AS
SELECT raw_service.FILE_KEY, raw_service.SOURCE_URL, raw_service.SOURCE_FILENAME, raw_service.SOURCE_POSITION AS SERVICE_POSITION,
       UHC_TEXT(raw_service.DOCUMENT:name) AS NAME, UHC_TEXT(raw_service.DOCUMENT:description) AS DESCRIPTION,
       UHC_TEXT(raw_service.DOCUMENT:billing_code) AS BILLING_CODE,
       UHC_TEXT(raw_service.DOCUMENT:billing_code_type) AS BILLING_CODE_TYPE,
       UHC_TEXT(raw_service.DOCUMENT:billing_code_type_version) AS BILLING_CODE_TYPE_VERSION,
       CONCAT(raw_service.FILE_KEY, ':', CAST(raw_service.SOURCE_POSITION AS VARCHAR), ':', CAST(allowed_item.INDEX AS VARCHAR), ':', CAST(payment_item.INDEX AS VARCHAR), ':', CAST(provider_item.INDEX AS VARCHAR)) AS OON_RATE_KEY,
       CAST(allowed_item.INDEX AS INTEGER) AS ALLOWED_POSITION, CAST(payment_item.INDEX AS INTEGER) AS PAYMENT_POSITION,
       CAST(provider_item.INDEX AS INTEGER) AS PROVIDER_POSITION,
       UHC_TEXT(allowed_item.VALUE:tin:type) AS TIN_TYPE, UHC_ID(allowed_item.VALUE:tin:value) AS TIN_VALUE,
       AS_ARRAY(allowed_item.VALUE:service_code) AS SERVICE_CODE, UHC_TEXT(allowed_item.VALUE:billing_class) AS BILLING_CLASS,
       CAST(UHC_AMOUNT(payment_item.VALUE:allowed_amount) AS NUMBER(18,6)) AS ALLOWED_AMOUNT,
       CAST(UHC_AMOUNT(provider_item.VALUE:billed_charge) AS NUMBER(18,6)) AS BILLED_CHARGE,
       TRANSFORM(AS_ARRAY(provider_item.VALUE:npi), x -> UHC_ID(x)) AS NPI
FROM V_RAW_PRICING raw_service, LATERAL FLATTEN(INPUT => raw_service.DOCUMENT:allowed_amounts) allowed_item,
     LATERAL FLATTEN(INPUT => allowed_item.VALUE:payments) payment_item,
     LATERAL FLATTEN(INPUT => payment_item.VALUE:providers) provider_item
WHERE raw_service.SECTION = 'out_of_network';
