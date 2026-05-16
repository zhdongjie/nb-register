package main

import "google.golang.org/protobuf/types/known/structpb"

func protoData(data map[string]any) *structpb.Struct {
	if len(data) == 0 {
		return nil
	}
	out, err := structpb.NewStruct(data)
	if err != nil {
		out, _ = structpb.NewStruct(map[string]any{"marshal_error": err.Error()})
	}
	return out
}

func protoDataMap(data *structpb.Struct) map[string]any {
	if data == nil {
		return nil
	}
	return data.AsMap()
}
