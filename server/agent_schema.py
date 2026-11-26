"""Version 3 strict nested response schemas and independent value validation."""
import json
import math

TYPES=('string','number','integer','boolean','object','array','null')
MAX_BYTES=32768
MAX_DEPTH=8
MAX_FIELDS=64
CONTRACT='Agent revision3 keeps route/model/effort/instructions/user and adds responseSchema. Root object; nested closed objects and arrays with items; scalar string,number,integer,boolean,null; one type optionally paired with null. Every object lists all properties in required and additionalProperties:false. Descriptions, scalar enum, minLength/maxLength, minItems/maxItems, minimum/maximum/exclusiveMinimum/exclusiveMaximum supported. Maximum64 total properties,8levels,32KiB schema/output. No $ref/$defs/anyOf/pattern/format or unrecognized keywords. Parsed values are independently checked; invalid results follow error with recorded diagnostics. Bind exact nested fields with values paths, including numeric array indexes; types are checked before execution. Null is not silently converted to text/number. Use revision2 for the existing flat scalar format, revision1 for text. Changing responseSchema through the block editor or side chat selects its format revision automatically; pins/version checks still apply.'


def types(spec):return spec['type'] if isinstance(spec['type'],list) else [spec['type']]


def kind(value):
    return {str:'string',int:'number',float:'number',bool:'boolean',dict:'object',list:'array',type(None):'null'}.get(type(value))


def compatible(value,choices):
    actual=kind(value)
    return actual in choices or actual=='number' and 'integer' in choices and (type(value) is int or value.is_integer())


def canonical(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'),sort_keys=True)


def finite(value):
    try:return math.isfinite(value)
    except OverflowError:return False


def equal(a,b):return kind(a)==kind(b) and a==b


def check(schema):
    try:raw=canonical(schema).encode('utf-8')
    except (ValueError,TypeError,RecursionError,UnicodeError):raise ValueError('Response schema must be finite JSON with valid Unicode.') from None
    if len(raw)>MAX_BYTES:raise ValueError('Response schema must fit within 32 KiB.')
    count=0
    def visit(spec,depth,path):
        nonlocal count
        if depth>MAX_DEPTH:raise ValueError('Response schema nesting exceeds eight levels.')
        if not isinstance(spec,dict) or 'type' not in spec:raise ValueError(path+': declare a supported JSON type.')
        ts=types(spec)
        if (not isinstance(ts,list) or not all(isinstance(t,str) and t in TYPES for t in ts)
            or len(ts)!=len(set(ts)) or not 1<=len(ts)<=2 or len(ts)==2 and 'null' not in ts):
            raise ValueError(path+': use one JSON type, optionally paired with null.')
        primary=next((t for t in ts if t!='null'),'null')
        allowed={'type','description'}|({'properties','required','additionalProperties'} if primary=='object' else
            {'items','minItems','maxItems'} if primary=='array' else {'enum'} if primary in ('boolean','null') else
            {'enum','minLength','maxLength'} if primary=='string' else {'enum','minimum','maximum','exclusiveMinimum','exclusiveMaximum'})
        if set(spec)-allowed:raise ValueError(path+': unsupported schema option '+sorted(set(spec)-allowed)[0]+'.')
        if 'description' in spec and (not isinstance(spec['description'],str) or len(spec['description'])>2000):raise ValueError(path+': description must be text up to 2000 characters.')
        if primary=='object':
            props=spec.get('properties');required=spec.get('required')
            if not isinstance(props,dict) or spec.get('additionalProperties') is not False:raise ValueError(path+': objects need properties and additionalProperties:false.')
            if not isinstance(required,list) or not all(isinstance(k,str) for k in required) or len(required)!=len(props) or set(required)!=set(props):raise ValueError(path+': require each property exactly once; use null for optional values.')
            count+=len(props)
            if count>MAX_FIELDS:raise ValueError('Response schema exceeds 64 total object properties.')
            for name,child in props.items():
                if not isinstance(name,str) or not name or len(name)>128 or name in ('__proto__','constructor','prototype'):raise ValueError(path+': choose a non-reserved property name of 1–128 characters.')
                visit(child,depth+1,path+'.'+name)
        elif primary=='array':
            if 'items' not in spec:raise ValueError(path+': arrays require an items schema.')
            visit(spec['items'],depth+1,path+'[]')
        pairs=[('minLength','maxLength')] if primary=='string' else [('minItems','maxItems')] if primary=='array' else [('minimum','maximum'),('exclusiveMinimum','exclusiveMaximum')]
        for low,high in pairs:
            for key in (low,high):
                if key not in spec:continue
                val=spec[key]
                if primary in ('string','array'):
                    if type(val) is not int or not 0<=val<=100000:raise ValueError(path+': '+key+' must be an integer from 0 to 100000.')
                elif type(val) not in (int,float) or not finite(val):raise ValueError(path+': '+key+' must be a finite number.')
            if low in spec and high in spec and spec[low]>spec[high]:raise ValueError(path+': minimum exceeds maximum.')
        if 'enum' in spec:
            values=spec['enum']
            if not isinstance(values,list) or not 1<=len(values)<=64 or any(not compatible(v,ts) for v in values):raise ValueError(path+': enum needs 1–64 values matching the declared type.')
            if any(type(v) in (int,float) and not finite(v) for v in values) or any(equal(a,b) for i,a in enumerate(values) for b in values[i+1:]):raise ValueError(path+': enum values must be finite and distinct.')
    visit(schema,0,'$')
    if schema['type']!='object':raise ValueError('The response root must be an object.')
    return schema


def validate(schema,value,path='$'):
    ts=types(schema)
    if not compatible(value,ts):raise ValueError(path+': response type must be '+ ' or '.join(ts)+'.')
    if type(value) in (int,float) and not finite(value):raise ValueError(path+': response must be finite.')
    if 'enum' in schema and not any(equal(value,v) for v in schema['enum']):raise ValueError(path+': value is outside the declared enum.')
    if value is None:return value
    if isinstance(value,dict):
        if value.keys()!=schema['properties'].keys():raise ValueError(path+': response properties differ from the schema.')
        for name,child in schema['properties'].items():validate(child,value[name],path+'.'+name)
    elif isinstance(value,list):
        for i,child in enumerate(value):validate(schema['items'],child,path+'['+str(i)+']')
    if isinstance(value,(str,list)):
        low,high=('minLength','maxLength') if isinstance(value,str) else ('minItems','maxItems')
        if low in schema and len(value)<schema[low] or high in schema and len(value)>schema[high]:raise ValueError(path+': response length is outside the declared bounds.')
    elif type(value) in (int,float):
        checks={'minimum':lambda x:value>=x,'maximum':lambda x:value<=x,'exclusiveMinimum':lambda x:value>x,'exclusiveMaximum':lambda x:value<x}
        if any(not fn(schema[k]) for k,fn in checks.items() if k in schema):raise ValueError(path+': response number is outside the declared bounds.')
    return value


def at(schema,path):
    """Return the declared shape; a missing/nullable parent is still checked at run time."""
    spec=schema
    for key in path:
        ts=types(spec)
        if type(key) is str and 'object' in ts:
            extra=spec.get('additionalProperties')
            spec=spec.get('properties',{}).get(key,extra if isinstance(extra,dict) else None)
        elif type(key) is int and 'array' in ts:spec=spec.get('items')
        else:return None
        if spec is None:return None
    return spec


def binding_types(spec):return ['number' if t=='integer' else t for t in types(spec)]
