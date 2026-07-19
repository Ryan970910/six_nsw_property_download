CREATE OR REPLACE FUNCTION propdb_staging.normalise_banner_address(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    result text;
    replacement text[];
BEGIN
    IF value IS NULL THEN
        RETURN NULL;
    END IF;

    result := upper(btrim(value));
    result := regexp_replace(result, '[[:space:]]*/[[:space:]]*', '/', 'g');
    FOREACH replacement SLICE 1 IN ARRAY ARRAY[
        ['ST', 'STREET'], ['RD', 'ROAD'], ['AV', 'AVENUE'], ['AVE', 'AVENUE'],
        ['DR', 'DRIVE'], ['CCT', 'CIRCUIT'], ['CIR', 'CIRCUIT'], ['PL', 'PLACE'],
        ['CT', 'COURT'], ['CRT', 'COURT'], ['CRES', 'CRESCENT'], ['PDE', 'PARADE'],
        ['HWY', 'HIGHWAY'], ['LN', 'LANE'], ['CL', 'CLOSE'], ['TCE', 'TERRACE'],
        ['BVD', 'BOULEVARD']
    ]::text[][] LOOP
        result := regexp_replace(
            result,
            '(^|[[:space:]])' || replacement[1] || '([[:space:]]*,)',
            '\1' || replacement[2] || '\2',
            'g'
        );
    END LOOP;
    result := regexp_replace(result, '[,.]', ' ', 'g');
    RETURN NULLIF(regexp_replace(btrim(result), '[[:space:]]+', ' ', 'g'), '');
END;
$$;

CREATE OR REPLACE FUNCTION propdb_staging.banner_address_has_unit_number(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(value ~ '^[[:space:]]*[A-Za-z0-9-]+[[:space:]]*/', false)
$$;

CREATE OR REPLACE FUNCTION propdb_staging.banner_address_unit_number(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT NULLIF(substring(value FROM '^[[:space:]]*([A-Za-z0-9-]+)[[:space:]]*/'), '')
$$;

CREATE OR REPLACE FUNCTION propdb_staging.normalise_banner_base_address(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT propdb_staging.normalise_banner_address(
        regexp_replace(
            value,
            '^[[:space:]]*[A-Za-z0-9-]+[[:space:]]*/[[:space:]]*',
            ''
        )
    )
$$;

CREATE TEMP TABLE banner_address_build_source ON COMMIT DROP AS
SELECT
    prop_id,
    address AS original_address,
    propdb_staging.normalise_banner_base_address(address) AS normalized_base_address,
    propdb_staging.normalise_banner_address(address) AS normalized_full_address,
    propdb_staging.banner_address_has_unit_number(address) AS has_unit_number,
    propdb_staging.banner_address_unit_number(address) AS unit_number
FROM propdb_staging.data_universe;

CREATE INDEX idx_banner_address_build_source_base
    ON banner_address_build_source (normalized_base_address);
CREATE INDEX idx_banner_address_build_source_full
    ON banner_address_build_source (normalized_full_address);

CREATE TABLE propdb_staging.banner_address AS
SELECT
    md5(normalized_base_address)::uuid AS baid,
    normalized_base_address AS normalized_address,
    count(DISTINCT prop_id)::integer AS prop_id_count,
    count(DISTINCT normalized_full_address) FILTER (
        WHERE has_unit_number
    )::integer AS unit_address_count,
    bool_or(has_unit_number) AS has_unit_addresses,
    now() AS generated_at
FROM banner_address_build_source
WHERE normalized_base_address IS NOT NULL
GROUP BY normalized_base_address;

ALTER TABLE propdb_staging.banner_address
    ADD CONSTRAINT banner_address_pkey PRIMARY KEY (baid),
    ADD CONSTRAINT banner_address_normalized_address_key UNIQUE (normalized_address);
CREATE INDEX idx_banner_address_prop_id_count
    ON propdb_staging.banner_address (prop_id_count DESC);

CREATE TABLE propdb_staging.banner_address_unit AS
SELECT
    md5(source.normalized_full_address)::uuid AS unit_address_id,
    address.baid,
    source.normalized_full_address AS normalized_unit_address,
    min(source.unit_number) AS unit_number,
    count(DISTINCT source.prop_id)::integer AS prop_id_count,
    now() AS generated_at
FROM banner_address_build_source source
JOIN propdb_staging.banner_address address
    ON address.normalized_address = source.normalized_base_address
WHERE source.has_unit_number
  AND source.normalized_full_address IS NOT NULL
GROUP BY address.baid, source.normalized_full_address;

ALTER TABLE propdb_staging.banner_address_unit
    ADD CONSTRAINT banner_address_unit_pkey PRIMARY KEY (unit_address_id),
    ADD CONSTRAINT banner_address_unit_normalized_address_key UNIQUE (normalized_unit_address),
    ADD CONSTRAINT banner_address_unit_baid_fkey FOREIGN KEY (baid)
        REFERENCES propdb_staging.banner_address (baid);
CREATE INDEX idx_banner_address_unit_baid
    ON propdb_staging.banner_address_unit (baid);

CREATE TABLE propdb_staging.banner_address_prop_id_map AS
SELECT
    address.baid,
    source.prop_id,
    unit_address.unit_address_id,
    source.original_address,
    source.normalized_full_address,
    source.has_unit_number,
    source.unit_number,
    now() AS generated_at
FROM banner_address_build_source source
JOIN propdb_staging.banner_address address
    ON address.normalized_address = source.normalized_base_address
LEFT JOIN propdb_staging.banner_address_unit unit_address
    ON unit_address.normalized_unit_address = source.normalized_full_address
   AND source.has_unit_number;

ALTER TABLE propdb_staging.banner_address_prop_id_map
    ADD CONSTRAINT banner_address_prop_id_map_pkey PRIMARY KEY (baid, prop_id),
    ADD CONSTRAINT banner_address_prop_id_map_baid_fkey FOREIGN KEY (baid)
        REFERENCES propdb_staging.banner_address (baid),
    ADD CONSTRAINT banner_address_prop_id_map_unit_address_fkey FOREIGN KEY (unit_address_id)
        REFERENCES propdb_staging.banner_address_unit (unit_address_id);
CREATE INDEX idx_banner_address_prop_id_map_prop_id
    ON propdb_staging.banner_address_prop_id_map (prop_id);
CREATE INDEX idx_banner_address_prop_id_map_unit_address
    ON propdb_staging.banner_address_prop_id_map (unit_address_id)
    WHERE unit_address_id IS NOT NULL;
