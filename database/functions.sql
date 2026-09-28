-- Safe casts used by the API for numeric comparisons on tag values
-- (e.g. height, building:levels). Invalid values fall back to the default
-- instead of aborting the whole query.
CREATE OR REPLACE FUNCTION cast_to_float(text, double precision) RETURNS double precision AS $$
BEGIN
  RETURN $1::double precision;
EXCEPTION WHEN others THEN
  RETURN $2;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION cast_to_int(text, integer) RETURNS integer AS $$
BEGIN
  RETURN $1::integer;
EXCEPTION WHEN others THEN
  RETURN $2;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
